#!/usr/bin/env python3
"""Descriptor-safe verifier for the external DaemonSet admission state.

The admission process receives a read-only, root-owned registry.  Every state
generation is independently Ed25519 signed, content linked to its predecessor,
and contains the complete adopted agent and transition state.  Existing
DaemonSets are adopted by UID and canonical spec in this ledger; adoption does
not require annotating or otherwise mutating the Kubernetes object.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from daemonset_fence_policy import agent_contract, canonical, digest

REGISTRY_PATH = Path(
    "/var/lib/fs2-security-checkpoints-ro/daemonset-fence-runtime-state.json"
)
MAX_BYTES = 4 * 1024 * 1024
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
UID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _object(payload: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"{label} contains duplicate fields")
            result[key] = value
        return result

    value = json.loads(payload, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


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
            raise ValueError("DaemonSet runtime registry is not immutable and bounded")
        return payload
    finally:
        os.close(descriptor)


def _identity(value: object, label: str) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"username", "uid", "groups"}
        or not isinstance(value.get("username"), str)
        or not value["username"]
        or not isinstance(value.get("uid"), str)
        or not value["uid"]
        or not isinstance(value.get("groups"), list)
        or value["groups"] != sorted(set(value["groups"]))
        or any(not isinstance(group, str) or not group for group in value["groups"])
    ):
        raise ValueError(f"{label} identity is malformed")
    return value


def _agent(value: object, key: str, controller: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "namespace",
        "name",
        "uid",
        "daemonset_spec",
        "daemonset_spec_sha256",
        "pod_template",
        "pod_template_sha256",
        "maintenance_identity",
        "snapshot_generation",
        "snapshot_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected or "/" not in key:
        raise ValueError("DaemonSet runtime agent fields differ")
    namespace, name = key.split("/", 1)
    spec = value.get("daemonset_spec")
    if (
        value.get("namespace") != namespace
        or value.get("name") != name
        or not UID_RE.fullmatch(str(value.get("uid", "")))
        or not isinstance(spec, dict)
        or value.get("daemonset_spec_sha256") != digest(spec)
        or value.get("pod_template") != spec.get("template")
        or value.get("pod_template_sha256") != digest(spec.get("template"))
        or not re.fullmatch(
            r"s[0-9]{14}-[a-f0-9]{12}", str(value.get("snapshot_generation", ""))
        )
        or not SHA256_RE.fullmatch(str(value.get("snapshot_sha256", "")))
    ):
        raise ValueError("DaemonSet runtime agent identity or canonical spec differs")
    _identity(value.get("maintenance_identity"), f"{key} maintainer")
    return {**value, "daemonset_controller_identity": controller}


def _transition(value: object, active: dict[str, dict[str, Any]]) -> dict[str, Any]:
    fields = {
        "schema",
        "transition_id",
        "phase",
        "namespace",
        "predecessor_name",
        "predecessor_uid",
        "predecessor_snapshot_sha256",
        "successor_name",
        "successor_agent",
        "maintenance_identity",
        "prepared_readiness_contract_sha256",
        "successor_readiness",
        "successor_readiness_sha256",
        "transition_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema")
        != "fs2-serve.nebius.ai/daemonset-transition/v1"
        or value.get("phase")
        not in {"PREPARED", "ADMITTED", "SUCCESSOR_READY", "COMPLETED"}
        or not SHA256_RE.fullmatch(str(value.get("transition_id", "")))
        or not SHA256_RE.fullmatch(
            str(value.get("prepared_readiness_contract_sha256", ""))
        )
        or not SHA256_RE.fullmatch(str(value.get("transition_sha256", "")))
    ):
        raise ValueError("DaemonSet transition fields differ")
    key = f"{value.get('namespace')}/{value.get('predecessor_name')}"
    predecessor = active.get(key)
    if (
        predecessor is None
        or value.get("predecessor_uid") != predecessor.get("uid")
        or value.get("predecessor_snapshot_sha256")
        != predecessor.get("snapshot_sha256")
        or value.get("maintenance_identity")
        != predecessor.get("maintenance_identity")
    ):
        raise ValueError("DaemonSet transition predecessor is not the adopted agent")
    successor_key = f"{value.get('namespace')}/{value.get('successor_name')}"
    successor = _agent(
        value.get("successor_agent"),
        successor_key,
        predecessor["daemonset_controller_identity"],
    )
    if (
        value.get("successor_name") != value.get("predecessor_name")
        or successor.get("uid") != predecessor.get("uid")
    ):
        raise ValueError("DaemonSet transition must preserve its adopted name and UID")
    body = {
        key: item
        for key, item in value.items()
        if key not in {"transition_id", "transition_sha256"}
    }
    body_sha256 = digest(body)
    if value.get("transition_id") != body_sha256 or value.get("transition_sha256") != body_sha256:
        raise ValueError("DaemonSet transition ID is not bound to its full signed body")
    readiness = value.get("successor_readiness")
    if value["phase"] in {"SUCCESSOR_READY", "COMPLETED"}:
        if (
            not isinstance(readiness, dict)
            or value.get("successor_readiness_sha256") != digest(readiness)
            or readiness.get("uid") != successor.get("uid")
            or readiness.get("daemonset_spec_sha256")
            != successor.get("daemonset_spec_sha256")
            or readiness.get("available_number") != readiness.get("desired_number")
            or not isinstance(readiness.get("desired_number"), int)
            or readiness["desired_number"] < 1
        ):
            raise ValueError("DaemonSet successor readiness is not exact and complete")
    elif readiness is not None or value.get("successor_readiness_sha256") is not None:
        raise ValueError("unready DaemonSet transition carries self-asserted readiness")
    return {**value, "successor_agent": successor}


def load_verified_state(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    registry = _object(_safe_read(path), "DaemonSet runtime registry")
    if (
        set(registry)
        != {"schema", "checkpoint_public_key_pem", "anchor_sha256", "generations", "head_sha256"}
        or registry.get("schema")
        != "fs2-serve.nebius.ai/daemonset-fence-runtime-registry/v1"
        or not SHA256_RE.fullmatch(str(registry.get("anchor_sha256", "")))
        or not isinstance(registry.get("generations"), list)
        or not registry["generations"]
    ):
        raise ValueError("DaemonSet runtime registry fields differ")
    public_key = serialization.load_pem_public_key(
        str(registry["checkpoint_public_key_pem"]).encode()
    )
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("DaemonSet runtime checkpoint key is not Ed25519")
    predecessor = registry["anchor_sha256"]
    latest: dict[str, Any] | None = None
    for envelope in registry["generations"]:
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"body", "payload_sha256", "signature"}
            or not isinstance(envelope.get("body"), dict)
            or envelope["body"].get("predecessor_sha256") != predecessor
            or envelope.get("payload_sha256") != digest(envelope["body"])
        ):
            raise ValueError("DaemonSet runtime generation chain differs")
        try:
            public_key.verify(
                base64.b64decode(str(envelope["signature"]), validate=True),
                canonical(envelope["body"]),
            )
        except (InvalidSignature, TypeError, ValueError) as exc:
            raise ValueError("DaemonSet runtime generation signature is invalid") from exc
        predecessor = envelope["payload_sha256"]
        latest = envelope["body"]
    if registry.get("head_sha256") != predecessor or latest is None:
        raise ValueError("DaemonSet runtime registry head differs")
    required = {
        "schema",
        "cluster_id",
        "generation",
        "predecessor_sha256",
        "snapshot_ledger_head_sha256",
        "daemonset_controller_identity",
        "active_agents",
        "transitions",
        "issued_at",
        "valid_until",
    }
    if (
        set(latest) != required
        or latest.get("schema") != "fs2-serve.nebius.ai/daemonset-fence-runtime-state/v1"
        or not SHA256_RE.fullmatch(str(latest.get("snapshot_ledger_head_sha256", "")))
        or not isinstance(latest.get("active_agents"), dict)
        or not latest["active_agents"]
        or not isinstance(latest.get("transitions"), dict)
    ):
        raise ValueError("DaemonSet runtime state fields differ")
    controller = _identity(latest.get("daemonset_controller_identity"), "DaemonSet controller")
    active = {
        key: _agent(value, key, controller)
        for key, value in sorted(latest["active_agents"].items())
    }
    if latest["active_agents"] != agent_contract(active):
        raise ValueError("DaemonSet adopted-agent contract is not canonical")
    transitions = {
        key: _transition(value, active)
        for key, value in sorted(latest["transitions"].items())
    }
    if any(key != value["transition_id"] for key, value in transitions.items()):
        raise ValueError("DaemonSet transition map key differs from content ID")
    open_predecessors: set[str] = set()
    open_successors: set[str] = set()
    for transition in transitions.values():
        if transition["phase"] == "COMPLETED":
            continue
        predecessor_key = f"{transition['namespace']}/{transition['predecessor_name']}"
        successor_key = f"{transition['namespace']}/{transition['successor_name']}"
        if predecessor_key in open_predecessors or successor_key in open_successors:
            raise ValueError("more than one active transition exists for a DaemonSet")
        open_predecessors.add(predecessor_key)
        open_successors.add(successor_key)
    try:
        issued_at = datetime.fromisoformat(str(latest["issued_at"]).replace("Z", "+00:00"))
        valid_until = datetime.fromisoformat(
            str(latest["valid_until"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("DaemonSet runtime validity is not RFC3339") from exc
    now = datetime.now(UTC)
    if (
        issued_at.tzinfo is None
        or valid_until.tzinfo is None
        or issued_at.astimezone(UTC) > now
        or valid_until.astimezone(UTC) <= now
    ):
        raise ValueError("DaemonSet runtime state is not currently valid")
    return {
        "verified": True,
        "cluster_id": latest["cluster_id"],
        "generation": latest["generation"],
        "head_sha256": predecessor,
        "snapshot_ledger_head_sha256": latest["snapshot_ledger_head_sha256"],
        "daemonset_controller_identity": controller,
        "active_agents": active,
        "transitions": transitions,
    }
