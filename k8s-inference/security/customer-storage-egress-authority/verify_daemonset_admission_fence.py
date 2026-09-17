#!/usr/bin/env python3
"""Verify the separately owned, continuously enforced DaemonSet fence.

The fence is intentionally outside both Terraform identities in this program.
Its pinned adapter re-reads the canonical admission objects and append-only
snapshot ledger from the security owner's cluster/audit boundary.  Terraform
may consume the receipt, but cannot create, update, disable, or delete it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from daemonset_fence_policy import POLICY_SPEC, agent_contract

REGISTRY_PATH = Path(
    "/var/lib/fs2-security-checkpoints-ro/kubernetes-daemonset-admission-fence.json"
)
ADAPTER_PATH = Path("/usr/libexec/fs2-security/kubernetes-daemonset-admission-fence")
POLICY_SOURCE_PATH = Path(__file__).with_name("daemonset_fence_policy.py")
RUNTIME_SOURCE_PATH = Path(__file__).with_name("daemonset_fence_runtime.py")
SERVER_SOURCE_PATH = Path(__file__).with_name("daemonset_fence_server.py")
MAX_BYTES = 1024 * 1024
UID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
def _sha256(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _validate_binding(
    value: object,
    *,
    cluster_id: str,
    policy_uid: str,
    enforcer_bundle_sha256: str,
) -> dict[str, Any]:
    expected = {
        "schema": "fs2-serve.nebius.ai/daemonset-snapshot-fence-binding/v4",
        "cluster_id": cluster_id,
        "policy_uid": policy_uid,
        "scope": "Cluster",
        "namespace_exclusions": [],
        "object_selector": {},
        "failure_policy": "Fail",
        "match_policy": "Equivalent",
        "admission_review_versions": ["v1"],
        "side_effects": "NoneOnDryRun",
        "timeout_seconds": 5,
        "rules": [
            {
                "api_groups": ["apps"],
                "api_versions": ["v1"],
                "operations": ["CREATE", "UPDATE", "DELETE"],
                "resources": ["daemonsets"],
                "scope": "Namespaced",
            },
            {
                "api_groups": [""],
                "api_versions": ["v1"],
                "operations": ["CREATE"],
                "resources": ["pods"],
                "scope": "Namespaced",
            },
        ],
        "enforcer_bundle_sha256": enforcer_bundle_sha256,
    }
    if value != expected:
        raise ValueError("DaemonSet fence binding semantics differ")
    return expected


def _validate_snapshot_ledger(
    value: object,
    *,
    cluster_id: str,
    anchor_sha256: str,
    expected_agents: dict[str, dict[str, Any]],
) -> str:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema",
            "cluster_id",
            "anchor_sha256",
            "generations",
            "head_generation",
            "head_sha256",
        }
        or value.get("schema")
        != "fs2-serve.nebius.ai/daemonset-snapshot-ledger/v1"
        or value.get("cluster_id") != cluster_id
        or value.get("anchor_sha256") != anchor_sha256
        or not isinstance(value.get("generations"), list)
        or not value["generations"]
    ):
        raise ValueError("DaemonSet snapshot ledger identity differs")
    predecessor = anchor_sha256
    generations: set[str] = set()
    entries: list[dict[str, Any]] = []
    last_generation = ""
    for item in value["generations"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"generation", "predecessor_sha256", "entries"}
            or item.get("predecessor_sha256") != predecessor
            or not isinstance(item.get("entries"), list)
            or not item["entries"]
            or item["entries"]
            != sorted(
                item["entries"],
                key=lambda entry: (
                    str(entry.get("namespace", "")) if isinstance(entry, dict) else "",
                    str(entry.get("name", "")) if isinstance(entry, dict) else "",
                    str(entry.get("uid", "")) if isinstance(entry, dict) else "",
                    str(entry.get("snapshot_sha256", ""))
                    if isinstance(entry, dict)
                    else "",
                ),
            )
        ):
            raise ValueError("DaemonSet snapshot generation chain differs")
        generation = str(item.get("generation", ""))
        content = {
            "predecessor_sha256": item["predecessor_sha256"],
            "entries": item["entries"],
        }
        content_sha256 = _sha256(content)
        if (
            not re.fullmatch(r"s[0-9]{14}-[a-f0-9]{12}", generation)
            or generation[-12:] != content_sha256[:12]
            or generation in generations
        ):
            raise ValueError("DaemonSet snapshot generation is not content-bound")
        generation_entries: set[tuple[str, str, str]] = set()
        for entry in item["entries"]:
            fields = {
                "namespace",
                "name",
                "uid",
                "daemonset_spec_sha256",
                "pod_template_sha256",
                "owner_identity_sha256",
                "snapshot_generation",
                "snapshot_sha256",
            }
            if not isinstance(entry, dict) or set(entry) != fields:
                raise ValueError("DaemonSet snapshot entry fields differ")
            snapshot_body = {
                key: entry[key]
                for key in sorted(fields - {"snapshot_generation", "snapshot_sha256"})
            }
            identity = (
                str(entry.get("namespace", "")),
                str(entry.get("name", "")),
                str(entry.get("uid", "")),
            )
            if (
                not all(identity)
                or not UID_RE.fullmatch(identity[2])
                or identity in generation_entries
                or any(
                    not re.fullmatch(r"[a-f0-9]{64}", str(entry.get(field, "")))
                    for field in (
                        "daemonset_spec_sha256",
                        "pod_template_sha256",
                        "owner_identity_sha256",
                        "snapshot_sha256",
                    )
                )
                or entry["snapshot_sha256"] != _sha256(snapshot_body)
                or not re.fullmatch(
                    r"s[0-9]{14}-[a-f0-9]{12}",
                    str(entry.get("snapshot_generation", "")),
                )
                or str(entry["snapshot_generation"])[-12:]
                != str(entry["snapshot_sha256"])[0:12]
            ):
                raise ValueError("DaemonSet snapshot entry is invalid or duplicated")
            generation_entries.add(identity)
            entries.append(entry)
        generations.add(generation)
        predecessor = content_sha256
        last_generation = generation
    if (
        value.get("head_generation") != last_generation
        or value.get("head_sha256") != predecessor
    ):
        raise ValueError("DaemonSet snapshot ledger head differs from its chain")

    active = []
    for key, agent in sorted(expected_agents.items()):
        spec = agent.get("daemonset_spec")
        if (
            not isinstance(spec, dict)
            or "/" not in key
            or agent.get("daemonset_spec_sha256") != _sha256(spec)
            or not UID_RE.fullmatch(str(agent.get("uid", "")))
            or not re.fullmatch(
                r"s[0-9]{14}-[a-f0-9]{12}",
                str(agent.get("snapshot_generation", "")),
            )
            or not re.fullmatch(
                r"[a-f0-9]{64}", str(agent.get("snapshot_sha256", ""))
            )
            or not isinstance(agent.get("maintenance_identity"), dict)
        ):
            raise ValueError("signed active DaemonSet inventory is malformed")
        namespace, name = key.split("/", 1)
        active.append(
            {
                "namespace": namespace,
                "name": name,
                "uid": agent.get("uid"),
                "daemonset_spec_sha256": _sha256(spec),
                "pod_template_sha256": _sha256(spec.get("template")),
                "owner_identity_sha256": _sha256(
                    agent.get("maintenance_identity")
                ),
                "snapshot_generation": agent.get("snapshot_generation"),
                "snapshot_sha256": agent.get("snapshot_sha256"),
            }
        )
    if any(item not in entries for item in active):
        raise ValueError("active DaemonSet snapshot is absent from the append-only ledger")
    return predecessor


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _object(payload: bytes | str, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"{label} contains duplicate fields")
            value[key] = item
        return value

    result = json.loads(payload, object_pairs_hook=pairs)
    if not isinstance(result, dict):
        raise ValueError(f"{label} is not an object")
    return result


def _safe_read(path: Path) -> bytes:
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        before = os.fstat(descriptor)
        filesystem = os.fstatvfs(descriptor)
        payload = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_mode & 0o077
            or before.st_size <= 0
            or before.st_size > MAX_BYTES
            or not filesystem.f_flag & getattr(os, "ST_RDONLY", 1)
            or len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("DaemonSet fence registry is not immutable and bounded")
        return payload
    finally:
        os.close(descriptor)


def _source_sha256(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        payload = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > MAX_BYTES
            or len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("canonical DaemonSet policy source changed during read")
        return hashlib.sha256(payload).hexdigest()
    finally:
        os.close(descriptor)


def _source_bundle() -> tuple[dict[str, str], str]:
    sources = {
        "daemonset_fence_policy.py": _source_sha256(POLICY_SOURCE_PATH),
        "daemonset_fence_runtime.py": _source_sha256(RUNTIME_SOURCE_PATH),
        "daemonset_fence_server.py": _source_sha256(SERVER_SOURCE_PATH),
    }
    return sources, _sha256(sources)


def _open_adapter(expected_sha256: str) -> tuple[int, os.stat_result]:
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in ADAPTER_PATH.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            ADAPTER_PATH.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory
        )
    finally:
        os.close(directory)
    metadata = os.fstat(descriptor)
    filesystem = os.fstatvfs(descriptor)
    payload = os.read(descriptor, metadata.st_size + 1)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
        or not metadata.st_mode & 0o111
        or metadata.st_size <= 0
        or metadata.st_size > MAX_BYTES
        or not filesystem.f_flag & getattr(os, "ST_RDONLY", 1)
        or len(payload) != metadata.st_size
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        os.close(descriptor)
        raise ValueError("DaemonSet fence adapter differs from pinned custody")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return descriptor, metadata


def _fresh(value: object) -> None:
    try:
        observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("DaemonSet fence timestamp is not RFC3339") from exc
    if observed.tzinfo is None:
        raise ValueError("DaemonSet fence timestamp has no timezone")
    now = datetime.now(UTC)
    observed = observed.astimezone(UTC)
    if observed > now or now - observed > timedelta(minutes=5):
        raise ValueError("DaemonSet fence evidence is future-dated or stale")


def verify_live_daemonset_admission_fence(
    cluster_id: str,
    *,
    expected_inventory_sha256: str,
    expected_list_resource_version: str,
    expected_agents: dict[str, dict[str, Any]],
    expected_controller_identity: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    if (
        not isinstance(expected_agents, dict)
        or not expected_agents
        or any(
            not isinstance(key, str)
            or "/" not in key
            or not isinstance(agent, dict)
            for key, agent in expected_agents.items()
        )
    ):
        raise ValueError("signed active DaemonSet inventory is absent or malformed")
    if (
        not isinstance(expected_controller_identity, dict)
        or set(expected_controller_identity) != {"username", "uid", "groups"}
        or not all(
            isinstance(expected_controller_identity.get(field), str)
            and expected_controller_identity[field]
            for field in ("username", "uid")
        )
        or not isinstance(expected_controller_identity.get("groups"), list)
        or expected_controller_identity["groups"]
        != sorted(set(expected_controller_identity["groups"]))
    ):
        raise ValueError("authenticated DaemonSet-controller identity is malformed")
    registry = _object(_safe_read(REGISTRY_PATH), "DaemonSet fence registry")
    if (
        set(registry)
        != {
            "schema",
            "checkpoint_public_key_pem",
            "adapter_sha256",
            "enforcer_bundle_sha256",
            "enforcer_image_digest",
            "runtime_state_head_sha256",
            "snapshot_ledger_anchor_sha256",
            "fence_receipt",
        }
        or registry.get("schema")
        != "fs2-serve.nebius.ai/kubernetes-daemonset-admission-fence-registry/v4"
        or not re.fullmatch(r"[a-f0-9]{64}", str(registry.get("adapter_sha256", "")))
        or not re.fullmatch(
            r"[a-f0-9]{64}",
            str(registry.get("enforcer_bundle_sha256", "")),
        )
        or not re.fullmatch(
            r"[^@]+@sha256:[a-f0-9]{64}", str(registry.get("enforcer_image_digest", ""))
        )
        or not re.fullmatch(
            r"[a-f0-9]{64}", str(registry.get("runtime_state_head_sha256", ""))
        )
        or not re.fullmatch(
            r"[a-f0-9]{64}",
            str(registry.get("snapshot_ledger_anchor_sha256", "")),
        )
    ):
        raise ValueError("DaemonSet fence registry fields differ")
    source_digests, source_bundle_sha256 = _source_bundle()
    policy_source_sha256 = source_digests["daemonset_fence_policy.py"]
    if registry["enforcer_bundle_sha256"] != source_bundle_sha256:
        raise ValueError("live DaemonSet enforcer is not the canonical source bundle")
    receipt = registry.get("fence_receipt")
    receipt_fields = {
        "schema",
        "cluster_id",
        "fence_generation",
        "policy_uid",
        "policy_resource_version",
        "policy_spec",
        "policy_spec_sha256",
        "policy_source_sha256",
        "source_digests",
        "source_bundle_sha256",
        "enforcer_image_digest",
        "runtime_state_head_sha256",
        "agent_contract_sha256",
        "daemonset_controller_identity_sha256",
        "binding_uid",
        "binding_resource_version",
        "binding_spec",
        "binding_spec_sha256",
        "snapshot_ledger",
        "daemonset_inventory_sha256",
        "daemonset_list_resource_version",
        "snapshot_ledger_head_sha256",
        "continuous_enforcement",
        "observed_at",
        "payload_sha256",
        "signature",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != receipt_fields
        or receipt.get("schema")
        != "fs2-serve.nebius.ai/kubernetes-daemonset-admission-fence/v4"
        or receipt.get("cluster_id") != cluster_id
        or not re.fullmatch(
            r"f[0-9]{14}-[a-f0-9]{12}", str(receipt.get("fence_generation", ""))
        )
        or receipt.get("continuous_enforcement") is not True
        or receipt.get("daemonset_inventory_sha256") != expected_inventory_sha256
        or receipt.get("daemonset_list_resource_version")
        != expected_list_resource_version
        or receipt.get("policy_spec") != POLICY_SPEC
        or receipt.get("policy_spec_sha256") != _sha256(POLICY_SPEC)
        or receipt.get("policy_source_sha256") != policy_source_sha256
        or receipt.get("source_digests") != source_digests
        or receipt.get("source_bundle_sha256") != source_bundle_sha256
        or receipt.get("enforcer_image_digest") != registry["enforcer_image_digest"]
        or receipt.get("runtime_state_head_sha256")
        != registry["runtime_state_head_sha256"]
        or receipt.get("agent_contract_sha256") != _sha256(agent_contract(expected_agents))
        or receipt.get("daemonset_controller_identity_sha256")
        != _sha256(expected_controller_identity)
    ):
        raise ValueError("DaemonSet fence receipt identity or inventory differs")
    binding_spec = _validate_binding(
        receipt.get("binding_spec"),
        cluster_id=cluster_id,
        policy_uid=str(receipt.get("policy_uid", "")),
        enforcer_bundle_sha256=str(registry["enforcer_bundle_sha256"]),
    )
    if receipt.get("binding_spec_sha256") != _sha256(binding_spec):
        raise ValueError("DaemonSet fence binding digest differs from canonical bytes")
    ledger_head = _validate_snapshot_ledger(
        receipt.get("snapshot_ledger"),
        cluster_id=cluster_id,
        anchor_sha256=str(registry["snapshot_ledger_anchor_sha256"]),
        expected_agents=expected_agents,
    )
    if receipt.get("snapshot_ledger_head_sha256") != ledger_head:
        raise ValueError("DaemonSet fence receipt does not bind the recomputed ledger")
    fence_content = {
        "cluster_id": cluster_id,
        "policy_uid": receipt["policy_uid"],
        "policy_spec_sha256": receipt["policy_spec_sha256"],
        "policy_source_sha256": receipt["policy_source_sha256"],
        "source_bundle_sha256": receipt["source_bundle_sha256"],
        "enforcer_image_digest": receipt["enforcer_image_digest"],
        "runtime_state_head_sha256": receipt["runtime_state_head_sha256"],
        "agent_contract_sha256": receipt["agent_contract_sha256"],
        "daemonset_controller_identity_sha256": receipt[
            "daemonset_controller_identity_sha256"
        ],
        "binding_uid": receipt["binding_uid"],
        "binding_spec_sha256": receipt["binding_spec_sha256"],
        "snapshot_ledger_head_sha256": ledger_head,
        "daemonset_inventory_sha256": receipt["daemonset_inventory_sha256"],
        "daemonset_list_resource_version": receipt[
            "daemonset_list_resource_version"
        ],
        "enforcer_bundle_sha256": registry["enforcer_bundle_sha256"],
    }
    if str(receipt["fence_generation"])[-12:] != _sha256(fence_content)[:12]:
        raise ValueError("DaemonSet fence generation is not content-bound")
    for field in (
        "policy_spec_sha256",
        "policy_source_sha256",
        "source_bundle_sha256",
        "runtime_state_head_sha256",
        "agent_contract_sha256",
        "daemonset_controller_identity_sha256",
        "binding_spec_sha256",
        "daemonset_inventory_sha256",
        "snapshot_ledger_head_sha256",
    ):
        if not re.fullmatch(r"[a-f0-9]{64}", str(receipt.get(field, ""))):
            raise ValueError(f"DaemonSet fence {field} is invalid")
    for field in (
        "policy_uid",
        "binding_uid",
    ):
        if not UID_RE.fullmatch(str(receipt.get(field, ""))):
            raise ValueError(f"DaemonSet fence {field} is not a Kubernetes UID")
    for field in (
        "policy_resource_version",
        "binding_resource_version",
    ):
        if not isinstance(receipt.get(field), str) or not receipt[field]:
            raise ValueError(f"DaemonSet fence {field} is absent")
    body = {
        key: value
        for key, value in receipt.items()
        if key not in {"payload_sha256", "signature"}
    }
    body_sha256 = hashlib.sha256(canonical(body)).hexdigest()
    if receipt.get("payload_sha256") != body_sha256:
        raise ValueError("DaemonSet fence receipt digest differs")
    key = serialization.load_pem_public_key(
        str(registry["checkpoint_public_key_pem"]).encode()
    )
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("DaemonSet fence checkpoint key is not Ed25519")
    try:
        key.verify(
            base64.b64decode(str(receipt["signature"]), validate=True),
            canonical(body),
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise ValueError("DaemonSet fence receipt signature is invalid") from exc
    _fresh(receipt.get("observed_at"))

    descriptor, before = _open_adapter(str(registry["adapter_sha256"]))
    request = {
        "schema": "fs2-serve.nebius.ai/kubernetes-daemonset-admission-fence-query/v4",
        "cluster_id": cluster_id,
        "fence_generation": receipt["fence_generation"],
        "policy_uid": receipt["policy_uid"],
        "binding_uid": receipt["binding_uid"],
        "policy_spec_sha256": receipt["policy_spec_sha256"],
        "policy_source_sha256": receipt["policy_source_sha256"],
        "source_bundle_sha256": receipt["source_bundle_sha256"],
        "enforcer_image_digest": receipt["enforcer_image_digest"],
        "runtime_state_head_sha256": receipt["runtime_state_head_sha256"],
        "agent_contract_sha256": receipt["agent_contract_sha256"],
        "daemonset_controller_identity_sha256": receipt[
            "daemonset_controller_identity_sha256"
        ],
        "binding_spec_sha256": receipt["binding_spec_sha256"],
        "snapshot_ledger_head_sha256": receipt["snapshot_ledger_head_sha256"],
    }
    try:
        result = subprocess.run(
            [f"/proc/self/fd/{descriptor}"],
            input=json.dumps(request, sort_keys=True, separators=(",", ":")),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            pass_fds=(descriptor,),
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        result.returncode != 0
        or not result.stdout
        or len(result.stdout.encode()) > 8 * MAX_BYTES
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError("DaemonSet fence adapter failed or changed during verification")
    live = _object(result.stdout, "DaemonSet fence adapter response")
    expected_live = {
        "schema": "fs2-serve.nebius.ai/kubernetes-daemonset-admission-fence-evidence/v4",
        "cluster_id": cluster_id,
        "fence_generation": receipt["fence_generation"],
        "policy_uid": receipt["policy_uid"],
        "policy_resource_version": receipt["policy_resource_version"],
        "policy_spec": receipt["policy_spec"],
        "policy_source_sha256": receipt["policy_source_sha256"],
        "source_digests": receipt["source_digests"],
        "source_bundle_sha256": receipt["source_bundle_sha256"],
        "enforcer_image_digest": receipt["enforcer_image_digest"],
        "runtime_state_head_sha256": receipt["runtime_state_head_sha256"],
        "agent_contract_sha256": receipt["agent_contract_sha256"],
        "daemonset_controller_identity_sha256": receipt[
            "daemonset_controller_identity_sha256"
        ],
        "binding_uid": receipt["binding_uid"],
        "binding_resource_version": receipt["binding_resource_version"],
        "binding_spec": receipt["binding_spec"],
        "snapshot_ledger": receipt["snapshot_ledger"],
        "daemonset_inventory_sha256": receipt["daemonset_inventory_sha256"],
        "daemonset_list_resource_version": receipt[
            "daemonset_list_resource_version"
        ],
        "continuous_enforcement": True,
    }
    if (
        live != expected_live
        or _sha256(live["policy_spec"]) != receipt["policy_spec_sha256"]
        or _sha256(live["binding_spec"]) != receipt["binding_spec_sha256"]
        or _validate_snapshot_ledger(
            live["snapshot_ledger"],
            cluster_id=cluster_id,
            anchor_sha256=str(registry["snapshot_ledger_anchor_sha256"]),
            expected_agents=expected_agents,
        )
        != receipt["snapshot_ledger_head_sha256"]
    ):
        raise ValueError(
            "live DaemonSet fence semantics or ledger differ from canonical custody"
        )
    return receipt, body_sha256
