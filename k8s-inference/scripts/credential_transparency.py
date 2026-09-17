#!/usr/bin/env python3
"""Verify source-pinned append-only transparency checkpoints.

This module intentionally contains no network or signing code.  The credential
authority is only a producer.  Admission depends on a source-reviewed trust
bundle, an RFC6962-style inclusion proof, consistency from the source-pinned
prior checkpoint, and independent witness signatures over the new checkpoint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

HEX64 = re.compile(r"^[0-9a-f]{64}$")


class TransparencyVerificationError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def leaf_hash(record: Any) -> bytes:
    return hashlib.sha256(b"\x00" + canonical_bytes(record)).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _hash_bytes(value: Any, *, label: str) -> bytes:
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        raise TransparencyVerificationError(f"{label} must be lowercase SHA-256")
    return bytes.fromhex(value)


def _proof(value: Any, *, label: str) -> list[bytes]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or HEX64.fullmatch(item) is None for item in value
    ):
        raise TransparencyVerificationError(f"{label} is malformed")
    return [bytes.fromhex(item) for item in value]


def _signature(value: Any, *, label: str) -> bytes:
    if not isinstance(value, str):
        raise TransparencyVerificationError(f"{label} is absent")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as error:
        raise TransparencyVerificationError(f"{label} is malformed") from error
    if len(decoded) != 64:
        raise TransparencyVerificationError(f"{label} must be Ed25519")
    return decoded


def _key_id(key: Any) -> str:
    return hashlib.sha256(
        key.public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)
    ).hexdigest()


def verify_inclusion(
    *, leaf: bytes, leaf_index: int, tree_size: int, proof: list[bytes], root: bytes
) -> None:
    """Verify an RFC6962 audit path for one exact leaf."""

    if tree_size < 1 or leaf_index < 0 or leaf_index >= tree_size:
        raise TransparencyVerificationError("transparency leaf position is invalid")
    index = leaf_index
    last = tree_size - 1
    calculated = leaf
    for sibling in proof:
        if index & 1 or index == last:
            calculated = node_hash(sibling, calculated)
            while index and not (index & 1):
                index >>= 1
                last >>= 1
        else:
            calculated = node_hash(calculated, sibling)
        index >>= 1
        last >>= 1
    if last != 0 or calculated != root:
        raise TransparencyVerificationError("transparency inclusion proof is invalid")


def verify_consistency(
    *,
    old_size: int,
    new_size: int,
    old_root: bytes,
    new_root: bytes,
    proof: list[bytes],
) -> None:
    """Verify an RFC6962 consistency proof from an accepted prior head."""

    if old_size < 1 or new_size < old_size:
        raise TransparencyVerificationError("transparency tree sizes are invalid")
    if old_size == new_size:
        if proof or old_root != new_root:
            raise TransparencyVerificationError(
                "equal-size transparency checkpoints are inconsistent"
            )
        return
    old_index = old_size - 1
    new_index = new_size - 1
    while old_index & 1:
        old_index >>= 1
        new_index >>= 1
    if not proof:
        raise TransparencyVerificationError("transparency consistency proof is absent")
    if old_index == 0:
        old_calculated = old_root
        new_calculated = old_root
        remaining = proof
    else:
        old_calculated = proof[0]
        new_calculated = proof[0]
        remaining = proof[1:]
    for sibling in remaining:
        if old_index & 1 or old_index == new_index:
            old_calculated = node_hash(sibling, old_calculated)
            new_calculated = node_hash(sibling, new_calculated)
            while old_index and not (old_index & 1):
                old_index >>= 1
                new_index >>= 1
        else:
            new_calculated = node_hash(new_calculated, sibling)
        old_index >>= 1
        new_index >>= 1
    if (
        old_index != 0
        or new_index != 0
        or old_calculated != old_root
        or new_calculated != new_root
    ):
        raise TransparencyVerificationError(
            "transparency checkpoint does not extend the accepted prior head"
        )


def verify_checkpoint(
    *,
    anchor: dict[str, Any],
    record: dict[str, Any],
    trust: dict[str, Any],
    load_public_key: Callable[[str, str], Any],
) -> dict[str, Any]:
    """Verify the log checkpoint, record inclusion, prior head and witnesses."""

    checkpoint = anchor.get("checkpoint")
    inclusion = anchor.get("inclusion_proof")
    consistency = anchor.get("consistency_proof")
    witnesses = anchor.get("witnesses")
    expected_anchor_fields = {
        "schema",
        "endpoint",
        "log_id",
        "entry_index",
        "claim_sha256",
        "record_sha256",
        "leaf_sha256",
        "anchored_at",
        "retention_until",
        "checkpoint",
        "inclusion_proof",
        "consistency_proof",
        "witnesses",
    }
    checkpoint_fields = {
        "schema",
        "log_id",
        "tree_size",
        "root_sha256",
        "previous_checkpoint_sha256",
        "issued_at",
        "anchor_key_id",
        "signature",
    }
    if (
        set(anchor) != expected_anchor_fields
        or anchor.get("schema")
        != "fs2-serve.nebius.ai/external-evidence-anchor/v2"
        or not isinstance(checkpoint, dict)
        or set(checkpoint) != checkpoint_fields
        or checkpoint.get("schema")
        != "fs2-serve.nebius.ai/transparency-checkpoint/v1"
        or anchor.get("endpoint") != trust.get("endpoint")
        or anchor.get("log_id") != trust.get("log_id")
        or checkpoint.get("log_id") != trust.get("log_id")
        or not isinstance(anchor.get("entry_index"), int)
        or anchor["entry_index"] < 0
        or not isinstance(checkpoint.get("tree_size"), int)
        or checkpoint["tree_size"] <= anchor["entry_index"]
    ):
        raise TransparencyVerificationError("external transparency anchor is malformed")

    unsigned_checkpoint = {
        key: value for key, value in checkpoint.items() if key != "signature"
    }
    prior = trust.get("trusted_checkpoint")
    if (
        not isinstance(prior, dict)
        or set(prior) != {"tree_size", "root_sha256", "checkpoint_sha256"}
        or not isinstance(prior.get("tree_size"), int)
        or prior["tree_size"] < 1
        or checkpoint.get("previous_checkpoint_sha256")
        != prior.get("checkpoint_sha256")
    ):
        raise TransparencyVerificationError(
            "transparency evidence is not bound to the source-pinned prior head"
        )
    root = _hash_bytes(checkpoint.get("root_sha256"), label="checkpoint root")
    old_root = _hash_bytes(prior.get("root_sha256"), label="prior checkpoint root")
    expected_leaf = leaf_hash(record)
    if anchor.get("leaf_sha256") != expected_leaf.hex():
        raise TransparencyVerificationError("transparency leaf differs from the record")
    verify_inclusion(
        leaf=expected_leaf,
        leaf_index=anchor["entry_index"],
        tree_size=checkpoint["tree_size"],
        proof=_proof(inclusion, label="transparency inclusion proof"),
        root=root,
    )
    verify_consistency(
        old_size=prior["tree_size"],
        new_size=checkpoint["tree_size"],
        old_root=old_root,
        new_root=root,
        proof=_proof(consistency, label="transparency consistency proof"),
    )

    anchor_key = load_public_key(
        "anchor", str(trust.get("anchor_public_key_sha256", ""))
    )
    if (
        checkpoint.get("anchor_key_id") != trust.get("anchor_key_id")
        or checkpoint.get("anchor_key_id") != _key_id(anchor_key)
    ):
        raise TransparencyVerificationError("transparency anchor key ID is not trusted")
    try:
        anchor_key.verify(
            _signature(checkpoint.get("signature"), label="checkpoint signature"),
            canonical_bytes(unsigned_checkpoint),
        )
    except InvalidSignature as error:
        raise TransparencyVerificationError("checkpoint signature is invalid") from error

    witness_trust = trust.get("witnesses")
    if not isinstance(witness_trust, list) or not isinstance(witnesses, list):
        raise TransparencyVerificationError("transparency witness set is absent")
    trusted_by_id = {
        item.get("witness_id"): item
        for item in witness_trust
        if isinstance(item, dict)
    }
    accepted: set[str] = set()
    for witness in witnesses:
        if not isinstance(witness, dict) or set(witness) != {
            "witness_id",
            "key_id",
            "signature",
        }:
            raise TransparencyVerificationError("witness statement is malformed")
        witness_id = witness.get("witness_id")
        trusted = trusted_by_id.get(witness_id)
        if (
            not isinstance(trusted, dict)
            or witness.get("key_id") != trusted.get("key_id")
            or witness_id in accepted
        ):
            raise TransparencyVerificationError("witness identity is not trusted")
        key = load_public_key(
            f"witness:{witness_id}", str(trusted.get("public_key_sha256", ""))
        )
        if witness.get("key_id") != _key_id(key):
            raise TransparencyVerificationError("witness key ID differs from its pin")
        try:
            key.verify(
                _signature(witness.get("signature"), label="witness signature"),
                canonical_bytes(unsigned_checkpoint),
            )
        except InvalidSignature as error:
            raise TransparencyVerificationError("witness signature is invalid") from error
        accepted.add(str(witness_id))
    if len(accepted) < trust.get("minimum_witnesses", 0):
        raise TransparencyVerificationError("independent witness quorum is absent")
    return checkpoint
