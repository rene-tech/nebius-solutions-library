#!/usr/bin/env python3
"""Verify the signed v3 external execution acknowledgement for Terraform.

This verifier is offline.  It accepts only the repository-pinned active trust
contract and executor/verifier sources, then validates the whole signed,
short-lived acknowledgement for the exact phase, consumer, action, receipt,
context, cluster, and Kubernetes identity requested by the rollout gate.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import sai07_authoritative_evidence as evidence
import verify_sai07_custody_manifest_bundle as bundle_v1
import verify_sai07_custody_trust_v3 as trust_v3

ROOT = Path(__file__).resolve().parents[1]
TRUST_LOCK = ROOT / "stages" / "pod-security-custody" / "custody-trust-lock-v3.json"
SCHEMA = "fs2-serve.nebius.ai/sai07-external-execution-acknowledgement/v3"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
ZERO_SHA256 = "0" * 64


class AckV3Error(ValueError):
    pass


def exact(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise AckV3Error(f"{label} fields differ from the v3 execution contract")
    return value


def digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise AckV3Error(f"{label} must be a lowercase SHA-256")
    return value


def instant(value: object, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AckV3Error(f"{label} must be a UTC RFC3339 instant")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise AckV3Error(f"{label} is malformed") from error
    if parsed.tzinfo != dt.UTC:
        raise AckV3Error(f"{label} is not UTC")
    return parsed


def validate(query: dict[str, str]) -> dict[str, str]:
    supplied_lock = Path(query["trust_lock_path"])
    if supplied_lock.resolve() != TRUST_LOCK.resolve():
        raise AckV3Error("acknowledgement verifier requires the repository-pinned v3 lock")
    contract_bytes, contract = trust_v3.load_json(
        supplied_lock, "v3 trust lock", 1024 * 1024, repository_document=True
    )
    if contract.get("activation") != "active":
        raise AckV3Error("repository-pinned v3 custody activation is blocked")
    executor = exact(
        contract.get("executor"),
        {
            "acknowledgement_max_bytes",
            "acknowledgement_name_prefix",
            "acknowledgement_namespace",
            "field_manager",
            "source_path",
            "source_sha256",
            "verifier_path",
            "verifier_sha256",
        },
        "v3 executor pin",
    )
    source_relative = Path(evidence.nonempty(executor["source_path"], "executor source path"))
    verifier_relative = Path(evidence.nonempty(executor["verifier_path"], "ack verifier path"))
    if (
        source_relative.is_absolute()
        or verifier_relative.is_absolute()
        or ".." in source_relative.parts
        or ".." in verifier_relative.parts
    ):
        raise AckV3Error("executor source pins must be repository-relative without traversal")
    source = ROOT / source_relative
    verifier = ROOT / verifier_relative
    if verifier.resolve() != Path(__file__).resolve():
        raise AckV3Error("repository contract selects another acknowledgement verifier")
    for path, expected, label in (
        (source, executor["source_sha256"], "executor"),
        (verifier, executor["verifier_sha256"], "ack verifier"),
    ):
        actual = hashlib.sha256(
            trust_v3.read_regular(path, f"{label} source", 4 * 1024 * 1024)
        ).hexdigest()
        if actual != digest(expected, f"{label} source SHA-256"):
            raise AckV3Error(f"{label} differs from the repository-pinned source")

    ack_bytes, ack = trust_v3.load_json(
        Path(query["ack_path"]),
        "external execution acknowledgement",
        executor["acknowledgement_max_bytes"],
    )
    exact(
        ack,
        {
            "acknowledgement",
            "action",
            "authority_key_id",
            "cluster_id",
            "collection_id",
            "consumer",
            "context_sha256",
            "contract_sha256",
            "executor_source_sha256",
            "expires_at",
            "issued_at",
            "kube_system_uid",
            "manifest_bundle_sha256",
            "phase",
            "platform_objects_after_sha256",
            "platform_objects_before_sha256",
            "platform_state_addresses_sha256",
            "platform_state_lineage",
            "platform_state_serial",
            "platform_state_version",
            "receipt_bundle_sha256",
            "receipt_consumption_sha256",
            "schema",
            "signature",
        },
        "external execution acknowledgement",
    )
    if ack["schema"] != SCHEMA:
        raise AckV3Error("external execution acknowledgement schema is unsupported")
    authority = exact(
        contract["authorities"]["manifest"],
        {"key_id", "principal_id", "public_key_path", "public_key_sha256"},
        "manifest authority",
    )
    key_path = Path(evidence.nonempty(authority["public_key_path"], "manifest public-key path"))
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    public_key = trust_v3.read_regular(key_path, "manifest authority public key", 65536)
    if hashlib.sha256(public_key).hexdigest() != digest(
        authority["public_key_sha256"], "manifest authority key SHA-256"
    ):
        raise AckV3Error("manifest authority key differs from the repository pin")
    bundle_v1.verify_signature(ack, public_key, authority["key_id"])

    issued = instant(ack["issued_at"], "acknowledgement issued_at")
    expires = instant(ack["expires_at"], "acknowledgement expires_at")
    now = dt.datetime.now(dt.UTC)
    if (
        issued > now + dt.timedelta(seconds=30)
        or now > expires
        or expires <= issued
        or expires - issued > dt.timedelta(minutes=10)
    ):
        raise AckV3Error("external execution acknowledgement is stale or has an excessive lifetime")
    expected = {
        "action": query["expected_action"],
        "cluster_id": query["cluster_id"],
        "consumer": query["expected_consumer"],
        "context_sha256": digest(query["expected_context_sha256"], "expected context SHA-256"),
        "contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
        "executor_source_sha256": executor["source_sha256"],
        "kube_system_uid": query["kube_system_uid"],
        "phase": query["expected_phase"],
        "receipt_bundle_sha256": digest(query["receipt_bundle_sha256"], "receipt bundle SHA-256"),
    }
    for field, expected_value in expected.items():
        if ack[field] != expected_value:
            raise AckV3Error(f"external execution acknowledgement {field} differs")
    for field in (
        "manifest_bundle_sha256",
        "platform_objects_after_sha256",
        "platform_objects_before_sha256",
        "platform_state_addresses_sha256",
        "receipt_consumption_sha256",
    ):
        digest(ack[field], field)
    if ack["platform_objects_before_sha256"] != ack["platform_objects_after_sha256"]:
        raise AckV3Error("Terraform-retained objects changed across external acknowledgement SSA")
    if ack["phase"] == "prepare":
        if (
            ack["receipt_bundle_sha256"] != ZERO_SHA256
            or ack["receipt_consumption_sha256"] != ZERO_SHA256
        ):
            raise AckV3Error("prepare acknowledgement unexpectedly claims a phase receipt")
    elif ack["receipt_consumption_sha256"] == ZERO_SHA256:
        raise AckV3Error("post-prepare acknowledgement omits external ledger consumption")

    live_ack = exact(
        ack["acknowledgement"],
        {
            "field_manager",
            "field_set_sha256",
            "name",
            "namespace",
            "object_sha256",
            "resource_version",
            "uid",
        },
        "live acknowledgement identity",
    )
    if (
        live_ack["field_manager"] != executor["field_manager"]
        or live_ack["namespace"] != executor["acknowledgement_namespace"]
        or not isinstance(live_ack["name"], str)
        or not live_ack["name"].startswith(executor["acknowledgement_name_prefix"])
        or not all(
            isinstance(live_ack[field], str) and live_ack[field]
            for field in ("uid", "resource_version")
        )
    ):
        raise AckV3Error(
            "live acknowledgement identity differs from the repository executor contract"
        )
    for field in ("field_set_sha256", "object_sha256"):
        digest(live_ack[field], f"acknowledgement {field}")
    if ack["authority_key_id"] != authority["key_id"]:
        raise AckV3Error("acknowledgement authority key differs from the repository pin")
    return {
        "acknowledgement_name": live_ack["name"],
        "acknowledgement_sha256": hashlib.sha256(ack_bytes).hexdigest(),
        "action": ack["action"],
        "bundle_sha256": ack["receipt_bundle_sha256"],
        "consumer": ack["consumer"],
        "phase": ack["phase"],
        "valid": "true",
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        required = {
            "ack_path",
            "cluster_id",
            "expected_action",
            "expected_consumer",
            "expected_context_sha256",
            "expected_phase",
            "kube_system_uid",
            "receipt_bundle_sha256",
            "trust_lock_path",
        }
        if (
            not isinstance(query, dict)
            or set(query) != required
            or not all(isinstance(query[field], str) for field in required)
        ):
            raise AckV3Error("external query fields differ from the v3 acknowledgement contract")
        result = validate(query)
    except (
        AckV3Error,
        bundle_v1.BundleError,
        evidence.EvidenceError,
        trust_v3.TrustV3Error,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"SAI-07 external execution acknowledgement rejected: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
