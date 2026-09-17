#!/usr/bin/env python3
"""Verify externally signed, append-only storage-reconciler cutover state."""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from daemonset_fence_runtime import _object, _safe_read
from storage_reconciler_cutover_policy import canonical, digest

REGISTRY_PATH = Path(
    "/var/lib/fs2-security-checkpoints-ro/storage-reconciler-cutover-runtime.json"
)
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
UID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
PHASES = {
    "PREPARED",
    "QUIESCE_PREDECESSOR",
    "PREDECESSOR_QUIESCED",
    "ACTIVATE_SUCCESSOR",
    "COMPLETED",
    "ROLLBACK_QUIESCE",
    "ROLLBACK_SUCCESSOR_QUIESCED",
    "ROLLBACK_ACTIVATE",
    "ROLLED_BACK",
}
PHASE_SUCCESSORS = {
    "PREPARED": {"QUIESCE_PREDECESSOR"},
    "QUIESCE_PREDECESSOR": {"PREDECESSOR_QUIESCED"},
    "PREDECESSOR_QUIESCED": {"ACTIVATE_SUCCESSOR"},
    "ACTIVATE_SUCCESSOR": {"COMPLETED"},
    "COMPLETED": {"PREPARED", "ROLLBACK_QUIESCE"},
    "ROLLBACK_QUIESCE": {"ROLLBACK_SUCCESSOR_QUIESCED"},
    "ROLLBACK_SUCCESSOR_QUIESCED": {"ROLLBACK_ACTIVATE"},
    "ROLLBACK_ACTIVATE": {"ROLLED_BACK"},
    "ROLLED_BACK": {"PREPARED"},
}


def _identity(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == {"username", "uid", "groups"}
        and isinstance(value.get("username"), str)
        and value["username"]
        and isinstance(value.get("uid"), str)
        and value["uid"]
        and isinstance(value.get("groups"), list)
        and value["groups"] == sorted(set(value["groups"]))
    )


def _verify_envelope(envelope: object, key: Ed25519PublicKey) -> dict[str, Any]:
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"body", "payload_sha256", "signature"}
        or not isinstance(envelope.get("body"), dict)
        or envelope.get("payload_sha256") != digest(envelope["body"])
    ):
        raise ValueError("storage cutover envelope fields differ")
    try:
        key.verify(
            base64.b64decode(str(envelope["signature"]), validate=True),
            canonical(envelope["body"]),
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise ValueError("storage cutover signature is invalid") from exc
    return envelope["body"]


def _receipt(value: object, expected_phase: str) -> bool:
    expected_replicas = {
        "QUIESCE_PREDECESSOR": (0, 0),
        "ACTIVATE_SUCCESSOR": (0, 1),
        "ROLLBACK_QUIESCE": (0, 0),
        "ROLLBACK_ACTIVATE": (1, 0),
    }
    predecessor_replicas, successor_replicas = expected_replicas[expected_phase]
    predecessor = value.get("predecessor") if isinstance(value, dict) else None
    successor = value.get("successor") if isinstance(value, dict) else None
    return bool(
        isinstance(value, dict)
        and value.get("schema")
        == "fs2-serve.nebius.ai/storage-reconciler-cutover-observation/v1"
        and value.get("phase") == expected_phase
        and SHA256_RE.fullmatch(str(value.get("observation_sha256", "")))
        and value.get("observation_sha256")
        == digest({key: item for key, item in value.items() if key != "observation_sha256"})
        and isinstance(predecessor, dict)
        and isinstance(successor, dict)
        and predecessor.get("replicas") == predecessor_replicas
        and successor.get("replicas") == successor_replicas
        and (
            predecessor_replicas != 0
            or all(
                predecessor.get(field) == 0
                for field in ("ready_replicas", "available_replicas", "unavailable_replicas")
            )
        )
        and (
            successor_replicas != 0
            or all(
                successor.get(field) == 0
                for field in ("ready_replicas", "available_replicas", "unavailable_replicas")
            )
        )
        and (
            successor_replicas != 1
            or (
                successor.get("ready_replicas") == 1
                and successor.get("available_replicas") == 1
                and successor.get("unavailable_replicas") == 0
            )
        )
        and (
            predecessor_replicas != 1
            or (
                predecessor.get("ready_replicas") == 1
                and predecessor.get("available_replicas") == 1
                and predecessor.get("unavailable_replicas") == 0
            )
        )
    )


def _transition(
    value: object,
    deployments: dict[str, Any],
    previous: dict[str, Any] | None,
    previous_state_head: str,
) -> dict[str, Any]:
    fields = {
        "schema",
        "transition_id",
        "phase",
        "predecessor_generation",
        "successor_generation",
        "predecessor_transition_sha256",
        "predecessor_quiescence",
        "predecessor_quiescence_sha256",
        "successor_readiness",
        "successor_readiness_sha256",
        "rollback",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema")
        != "fs2-serve.nebius.ai/storage-reconciler-cutover-transition/v1"
        or value.get("phase") not in PHASES
        or value.get("predecessor_generation") not in deployments
        or value.get("successor_generation") not in deployments
        or value.get("predecessor_generation") == value.get("successor_generation")
    ):
        raise ValueError("storage cutover transition fields differ")
    body = {key: item for key, item in value.items() if key != "transition_id"}
    if value.get("transition_id") != digest(body):
        raise ValueError("storage cutover transition is not content-bound")
    phase = value["phase"]
    prior_id = value.get("predecessor_transition_sha256")
    if previous is None:
        if phase != "PREPARED" or prior_id is not None:
            raise ValueError("first storage cutover transition is not PREPARED")
    else:
        if (
            phase not in PHASE_SUCCESSORS[previous["phase"]]
            or prior_id != previous["transition_id"]
        ):
            raise ValueError("storage cutover phase does not extend its predecessor")
        if phase != "PREPARED" and (
            value["predecessor_generation"] != previous["predecessor_generation"]
            or value["successor_generation"] != previous["successor_generation"]
        ):
            raise ValueError("storage cutover changed generations mid-transition")
        if phase == "PREPARED" and value["predecessor_generation"] not in {
            previous["predecessor_generation"],
            previous["successor_generation"],
        }:
            raise ValueError("next storage cutover does not start from a retained generation")
    quiescence = value.get("predecessor_quiescence")
    if phase in {
        "PREDECESSOR_QUIESCED",
        "ACTIVATE_SUCCESSOR",
        "COMPLETED",
        "ROLLBACK_QUIESCE",
        "ROLLBACK_SUCCESSOR_QUIESCED",
        "ROLLBACK_ACTIVATE",
        "ROLLED_BACK",
    }:
        if (
            not _receipt(quiescence, "QUIESCE_PREDECESSOR")
            or value.get("predecessor_quiescence_sha256") != digest(quiescence)
        ):
            raise ValueError("storage predecessor quiescence receipt differs")
        if phase == "PREDECESSOR_QUIESCED":
            if (
                previous is None
                or quiescence.get("transition_id") != previous["transition_id"]
                or quiescence.get("state_head_sha256") != previous_state_head
            ):
                raise ValueError("storage predecessor quiescence is not bound to the executed epoch")
        elif previous is not None and quiescence != previous.get("predecessor_quiescence"):
            raise ValueError("storage predecessor quiescence changed after admission")
    elif quiescence is not None or value.get("predecessor_quiescence_sha256") is not None:
        raise ValueError("storage cutover carries premature predecessor quiescence")
    readiness = value.get("successor_readiness")
    if phase in {
        "COMPLETED",
        "ROLLBACK_QUIESCE",
        "ROLLBACK_SUCCESSOR_QUIESCED",
        "ROLLBACK_ACTIVATE",
        "ROLLED_BACK",
    }:
        if (
            not _receipt(readiness, "ACTIVATE_SUCCESSOR")
            or value.get("successor_readiness_sha256") != digest(readiness)
        ):
            raise ValueError("storage successor readiness receipt differs")
        if phase == "COMPLETED":
            if (
                previous is None
                or readiness.get("transition_id") != previous["transition_id"]
                or readiness.get("state_head_sha256") != previous_state_head
            ):
                raise ValueError("storage successor readiness is not bound to the activation epoch")
        elif previous is not None and readiness != previous.get("successor_readiness"):
            raise ValueError("storage successor readiness changed after completion")
    elif readiness is not None or value.get("successor_readiness_sha256") is not None:
        raise ValueError("storage cutover carries premature successor readiness")
    rollback = value.get("rollback")
    if phase.startswith("ROLLBACK_") or phase == "ROLLED_BACK":
        if (
            not isinstance(rollback, dict)
            or set(rollback)
            != {
                "from_epoch",
                "successor_quiesced",
                "successor_quiescence",
                "successor_quiescence_sha256",
                "zero_inflight_actions_receipt_sha256",
                "schema_compatibility_receipt_sha256",
                "provider_continuity_receipt_sha256",
            }
            or not isinstance(rollback.get("from_epoch"), int)
            or rollback["from_epoch"] < 1
            or any(
                not SHA256_RE.fullmatch(str(rollback.get(field, "")))
                for field in {
                    "zero_inflight_actions_receipt_sha256",
                    "schema_compatibility_receipt_sha256",
                    "provider_continuity_receipt_sha256",
                }
            )
        ):
            raise ValueError("storage rollback criteria are incomplete")
        if phase == "ROLLBACK_QUIESCE":
            if (
                rollback.get("successor_quiesced") is not False
                or rollback.get("successor_quiescence") is not None
                or rollback.get("successor_quiescence_sha256") is not None
            ):
                raise ValueError("rollback quiescence cannot be asserted before execution")
        else:
            successor_quiescence = rollback.get("successor_quiescence")
            if (
                rollback.get("successor_quiesced") is not True
                or not _receipt(successor_quiescence, "ROLLBACK_QUIESCE")
                or rollback.get("successor_quiescence_sha256")
                != digest(successor_quiescence)
            ):
                raise ValueError("rollback successor quiescence receipt differs")
            if phase == "ROLLBACK_SUCCESSOR_QUIESCED":
                if (
                    previous is None
                    or successor_quiescence.get("transition_id")
                    != previous["transition_id"]
                    or successor_quiescence.get("state_head_sha256")
                    != previous_state_head
                ):
                    raise ValueError("rollback quiescence is not bound to the executed epoch")
            elif previous is not None and (
                previous.get("rollback") or {}
            ).get("successor_quiescence") != successor_quiescence:
                raise ValueError("rollback quiescence changed after admission")
    elif rollback is not None:
        raise ValueError("forward storage cutover carries rollback authority")
    return value


def _deployment(generation: str, deployment: object) -> dict[str, Any]:
    if (
        not re.fullmatch(r"r[0-9]{14}-[a-f0-9]{12}", generation)
        or not isinstance(deployment, dict)
        or set(deployment)
        != {
            "namespace",
            "name",
            "uid",
            "image_digest",
            "passive_spec",
            "passive_spec_sha256",
            "pod_template",
            "release_identity",
        }
        or not isinstance(deployment.get("namespace"), str)
        or not deployment["namespace"]
        or not isinstance(deployment.get("name"), str)
        or not deployment["name"]
        or not UID_RE.fullmatch(str(deployment.get("uid", "")))
        or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", str(deployment.get("image_digest", ""))
        )
        or (deployment.get("passive_spec") or {}).get("replicas") != 0
        or deployment.get("passive_spec_sha256")
        != digest(deployment.get("passive_spec"))
        or deployment.get("pod_template")
        != (deployment.get("passive_spec") or {}).get("template")
        or not _identity(deployment.get("release_identity"))
    ):
        raise ValueError("storage cutover Deployment contract differs")
    labels = ((deployment["pod_template"].get("metadata") or {}).get("labels") or {})
    containers = (deployment["pod_template"].get("spec") or {}).get("containers") or []
    if (
        labels.get("fs2.nebius.ai/storage-rollout-generation") != generation
        or len(containers) != 1
        or containers[0].get("image", "").split("@")[-1] != deployment["image_digest"]
    ):
        raise ValueError("storage cutover Deployment generation or image differs")
    return deployment


def load_verified_state(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    registry = _object(_safe_read(path), "storage reconciler cutover registry")
    if (
        set(registry)
        != {
            "schema",
            "checkpoint_public_key_pem",
            "anchor_sha256",
            "generations",
            "head_sha256",
            "activation_envelope",
            "source_bundle_sha256",
            "enforcer_image_digest",
        }
        or registry.get("schema")
        != "fs2-serve.nebius.ai/storage-reconciler-cutover-registry/v1"
        or not SHA256_RE.fullmatch(str(registry.get("anchor_sha256", "")))
        or not isinstance(registry.get("generations"), list)
        or not registry["generations"]
        or not SHA256_RE.fullmatch(str(registry.get("source_bundle_sha256", "")))
        or not re.fullmatch(
            r"[^@]+@sha256:[a-f0-9]{64}",
            str(registry.get("enforcer_image_digest", "")),
        )
    ):
        raise ValueError("storage cutover registry fields differ")
    public_key = serialization.load_pem_public_key(
        str(registry["checkpoint_public_key_pem"]).encode()
    )
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("storage cutover key is not Ed25519")
    predecessor = registry["anchor_sha256"]
    previous_epoch = 0
    latest: dict[str, Any] | None = None
    retained_deployments: dict[str, Any] = {}
    previous_transition: dict[str, Any] | None = None
    immutable_state: dict[str, Any] | None = None
    for envelope in registry["generations"]:
        body = _verify_envelope(envelope, public_key)
        fields = {
            "schema",
            "cluster_id",
            "activation_epoch",
            "predecessor_head_sha256",
            "authority_manifest_sha256",
            "active_generation",
            "deployments",
            "security_owner_identity",
            "deployment_controller_identity",
            "replicaset_controller_identity",
            "transition",
            "valid_from",
            "valid_until",
        }
        if (
            set(body) != fields
            or body.get("schema")
            != "fs2-serve.nebius.ai/storage-reconciler-cutover-state/v1"
            or body.get("predecessor_head_sha256") != predecessor
            or body.get("activation_epoch") != previous_epoch + 1
            or not isinstance(body.get("deployments"), dict)
            or not body["deployments"]
            or not set(retained_deployments).issubset(body["deployments"])
            or any(
                generation in retained_deployments
                and body["deployments"][generation] != retained_deployments[generation]
                for generation in retained_deployments
            )
        ):
            raise ValueError("storage cutover generation is forked or omits custody")
        deployments = {
            generation: _deployment(generation, deployment)
            for generation, deployment in sorted(body["deployments"].items())
        }
        if not SHA256_RE.fullmatch(str(body.get("authority_manifest_sha256", ""))) or not all(
            _identity(body.get(field))
            for field in (
                "security_owner_identity",
                "deployment_controller_identity",
                "replicaset_controller_identity",
            )
        ):
            raise ValueError("storage cutover authority identity differs")
        stable = {
            field: body[field]
            for field in (
                "cluster_id",
                "authority_manifest_sha256",
                "security_owner_identity",
                "deployment_controller_identity",
                "replicaset_controller_identity",
            )
        }
        if immutable_state is not None and stable != immutable_state:
            raise ValueError("storage cutover changed its cluster or authenticated identities")
        immutable_state = stable
        transition = _transition(
            body.get("transition"), deployments, previous_transition, predecessor
        )
        phase = transition["phase"]
        expected_active = (
            transition["successor_generation"]
            if phase in {"ACTIVATE_SUCCESSOR", "COMPLETED"}
            else transition["predecessor_generation"]
            if phase in {"PREPARED", "ROLLBACK_ACTIVATE", "ROLLED_BACK"}
            else None
        )
        if body.get("active_generation") != expected_active:
            raise ValueError("storage cutover active generation differs from its phase")
        predecessor = envelope["payload_sha256"]
        previous_epoch = body["activation_epoch"]
        retained_deployments = deployments
        previous_transition = transition
        latest = body
    if registry.get("head_sha256") != predecessor or latest is None:
        raise ValueError("storage cutover head differs")
    try:
        valid_from = datetime.fromisoformat(
            str(latest["valid_from"]).replace("Z", "+00:00")
        )
        valid_until = datetime.fromisoformat(
            str(latest["valid_until"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("storage cutover validity is not RFC3339") from exc
    now = datetime.now(UTC)
    if (
        valid_from.tzinfo is None
        or valid_until.tzinfo is None
        or valid_from.astimezone(UTC) > now
        or valid_until.astimezone(UTC) <= now
    ):
        raise ValueError("storage cutover state is stale or future-dated")
    activation = _verify_envelope(registry["activation_envelope"], public_key)
    activation_fields = {
        "schema",
        "cluster_id",
        "activation_epoch",
        "predecessor_epoch",
        "target_generation",
        "target_image_digest",
        "authority_manifest_sha256",
        "cutover_receipt_sha256",
        "phase",
        "valid_from",
        "valid_until",
        "rollback",
        "predecessor_head_sha256",
        "ledger_anchor_sha256",
        "state_head_sha256",
        "source_bundle_sha256",
        "enforcer_image_digest",
    }
    active_generation = latest["active_generation"]
    active_image = (
        latest["deployments"][active_generation]["image_digest"]
        if active_generation is not None
        else None
    )
    if (
        set(activation) != activation_fields
        or activation.get("schema")
        != "fs2-serve.nebius.ai/storage-reconciler-activation/v1"
        or activation.get("activation_epoch") != latest["activation_epoch"]
        or activation.get("predecessor_epoch") != latest["activation_epoch"] - 1
        or activation.get("cluster_id") != latest["cluster_id"]
        or activation.get("target_generation") != latest["active_generation"]
        or activation.get("target_image_digest") != active_image
        or activation.get("phase") != ("ACTIVE" if active_generation is not None else "QUIESCED")
        or activation.get("authority_manifest_sha256")
        != latest["authority_manifest_sha256"]
        or activation.get("cutover_receipt_sha256") != latest["transition"]["transition_id"]
        or not SHA256_RE.fullmatch(str(activation.get("predecessor_head_sha256", "")))
        or activation.get("ledger_anchor_sha256") != registry["anchor_sha256"]
        or activation.get("state_head_sha256") != registry["head_sha256"]
        or activation.get("source_bundle_sha256")
        != registry["source_bundle_sha256"]
        or activation.get("enforcer_image_digest")
        != registry["enforcer_image_digest"]
        or activation.get("rollback") != latest["transition"].get("rollback")
        or (
            activation.get("rollback") is not None
            and activation["rollback"].get("from_epoch")
            != activation.get("predecessor_epoch")
        )
    ):
        raise ValueError("runtime activation projection differs from cutover state")
    try:
        activation_from = datetime.fromisoformat(
            str(activation["valid_from"]).replace("Z", "+00:00")
        )
        activation_until = datetime.fromisoformat(
            str(activation["valid_until"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("runtime activation validity is not RFC3339") from exc
    if (
        activation_from.tzinfo is None
        or activation_until.tzinfo is None
        or activation_from.astimezone(UTC) > now
        or activation_until.astimezone(UTC) <= now
    ):
        raise ValueError("runtime activation projection is stale or future-dated")
    return {
        **latest,
        "verified": True,
        "head_sha256": predecessor,
        "registry_anchor_sha256": registry["anchor_sha256"],
        "activation_envelope": registry["activation_envelope"],
        "source_bundle_sha256": registry["source_bundle_sha256"],
        "enforcer_image_digest": registry["enforcer_image_digest"],
    }
