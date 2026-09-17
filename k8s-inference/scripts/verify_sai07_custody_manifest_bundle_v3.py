#!/usr/bin/env python3
"""Bind signed SAI-07 desired manifests to raw Terraform state semantics.

This verifier is intentionally offline. The v3 trust result carries the
non-secret object projection independently reconstructed from the exact raw
state. Every address, identity, state UID/resourceVersion, attribute digest and
security-relevant desired body must match. The unique short-lived epoch token
performs all immediate live reads later; no stable owner kubeconfig is accepted.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import sai07_custody_state_semantics as semantics
import verify_sai07_custody_manifest_bundle as v1
import verify_sai07_custody_manifest_bundle_v2 as v2

SCHEMA = "fs2-serve.nebius.ai/sai07-custody-manifest-bundle/v3"
STATE_SCHEMA = "fs2-serve.nebius.ai/sai07-platform-state-inventory/v3"


class BundleV3Error(ValueError):
    pass


def exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BundleV3Error(f"{label} fields differ from the v3 contract")
    return value


def legacy_validation(bundle: dict[str, Any], trust: dict[str, str]) -> dict[str, str]:
    legacy_objects = [
        {"manifest": item["manifest"], "live_identity": item["live_identity"]}
        for item in bundle["objects"]
    ]
    legacy_bundle = {
        key: value
        for key, value in bundle.items()
        if key not in {"platform_state", "objects", "objects_sha256", "schema"}
    }
    legacy_bundle.update(
        {
            "schema": v1.SCHEMA,
            "objects": legacy_objects,
            "objects_sha256": hashlib.sha256(v1.canonical(legacy_objects)).hexdigest(),
        }
    )
    owner_groups = json.loads(trust["owner_groups_json"])
    platform_groups = json.loads(trust["platform_groups_json"])
    if len(owner_groups) != 1 or len(platform_groups) != 1:
        raise BundleV3Error("desired-manifest validation requires singleton identity groups")
    return v1.validate(
        legacy_bundle,
        {
            "cluster_id": trust["cluster_id"],
            "kube_system_uid": trust["kube_system_uid"],
            "owner_username": trust["owner_username"],
            "owner_group": owner_groups[0],
            "platform_username": trust["platform_username"],
            "platform_group": platform_groups[0],
            "iam_boundary_sha256": trust["iam_receipt_sha256"],
        },
    )


def authoritative_objects(trust: dict[str, str]) -> list[dict[str, Any]]:
    try:
        objects = json.loads(trust["custody_state_objects_json"])
    except (KeyError, json.JSONDecodeError) as error:
        raise BundleV3Error("authoritative trust omits raw-state object semantics") from error
    if (
        not isinstance(objects, list)
        or not objects
        or hashlib.sha256(v1.canonical(objects)).hexdigest()
        != trust.get("state_objects_sha256")
        or len(objects) != int(trust["state_object_count"])
    ):
        raise BundleV3Error("raw-state object semantic aggregate differs from trust")
    addresses: list[str] = []
    for index, item in enumerate(objects):
        exact(
            item,
            {
                "api_version",
                "attributes_sha256",
                "desired_semantics",
                "desired_semantics_sha256",
                "kind",
                "name",
                "namespace",
                "resource_type",
                "resource_version",
                "state_address",
                "uid",
            },
            f"raw-state objects[{index}]",
        )
        if not isinstance(item["state_address"], str):
            raise BundleV3Error("raw-state object address is malformed")
        for field in ("attributes_sha256", "desired_semantics_sha256"):
            if not v2.SHA256_RE.fullmatch(str(item[field])):
                raise BundleV3Error(f"raw-state object {field} is malformed")
        desired = exact(
            item["desired_semantics"],
            {"api_version", "body", "kind", "metadata", "name", "namespace"},
            f"raw-state objects[{index}].desired_semantics",
        )
        if (
            desired["api_version"],
            desired["kind"],
            desired["namespace"],
            desired["name"],
        ) != (
            item["api_version"],
            item["kind"],
            item["namespace"],
            item["name"],
        ):
            raise BundleV3Error("raw-state desired semantics remap their object identity")
        if semantics.digest(desired) != item["desired_semantics_sha256"]:
            raise BundleV3Error("raw-state desired semantic digest differs")
        addresses.append(item["state_address"])
    if addresses != sorted(set(addresses)):
        raise BundleV3Error("raw-state object addresses are not sorted and unique")
    return objects


def validate(
    bundle: dict[str, Any], query: dict[str, str], trust: dict[str, str]
) -> dict[str, str]:
    exact(
        bundle,
        {
            "schema",
            "cluster_id",
            "run_id",
            "kube_system_uid",
            "issued_at",
            "expires_at",
            "owner",
            "platform_exclusion",
            "iam_boundary_sha256",
            "objects_sha256",
            "objects",
            "platform_state",
            "signature",
        },
        "manifest bundle",
    )
    if bundle["schema"] != SCHEMA:
        raise BundleV3Error("manifest bundle schema is unsupported")
    objects = bundle["objects"]
    if (
        not isinstance(objects, list)
        or not objects
        or hashlib.sha256(v1.canonical(objects)).hexdigest() != bundle["objects_sha256"]
    ):
        raise BundleV3Error("manifest object aggregate differs")
    desired_result = legacy_validation(bundle, trust)
    raw_objects = authoritative_objects(trust)
    raw_by_address = {item["state_address"]: item for item in raw_objects}

    state = exact(
        bundle["platform_state"],
        {
            "schema",
            "backend_receipt_sha256",
            "state_lineage",
            "state_serial",
            "state_object_version",
            "state_etag",
            "state_sha256",
            "derived_objects_sha256",
            "objects_sha256",
            "objects",
        },
        "platform state",
    )
    if state["schema"] != STATE_SCHEMA:
        raise BundleV3Error("platform-state inventory schema is unsupported")
    expected_state = {
        "backend_receipt_sha256": trust["backend_receipt_sha256"],
        "state_lineage": trust["state_lineage"],
        "state_serial": int(trust["state_serial"]),
        "state_object_version": trust["state_object_version"],
        "state_etag": trust["state_etag"],
        "state_sha256": trust["state_sha256"],
        "derived_objects_sha256": trust["state_objects_sha256"],
    }
    if any(state[field] != expected for field, expected in expected_state.items()):
        raise BundleV3Error("signed platform-state identity differs from raw backend state")
    state_objects = state["objects"]
    if (
        not isinstance(state_objects, list)
        or hashlib.sha256(v1.canonical(state_objects)).hexdigest()
        != state["objects_sha256"]
    ):
        raise BundleV3Error("signed live-state object aggregate differs")
    signed_by_address: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(state_objects):
        exact(
            item,
            {
                "state_address",
                "api_version",
                "kind",
                "namespace",
                "name",
                "uid",
                "resource_version",
                "object_sha256",
            },
            f"platform_state.objects[{index}]",
        )
        address = item["state_address"]
        if not isinstance(address, str) or address in signed_by_address:
            raise BundleV3Error("signed state address is invalid or duplicated")
        raw = raw_by_address.get(address)
        if raw is None:
            raise BundleV3Error("signed state address is absent from raw Terraform state")
        if (
            item["api_version"],
            item["kind"],
            item["namespace"],
            item["name"],
        ) != (
            raw["api_version"],
            raw["kind"],
            raw["namespace"],
            raw["name"],
        ):
            raise BundleV3Error("signed state identity is remapped from its raw address")
        if raw["uid"] is not None and item["uid"] != raw["uid"]:
            raise BundleV3Error("signed state UID differs from raw Terraform state")
        if (
            raw["resource_version"] is not None
            and item["resource_version"] != raw["resource_version"]
        ):
            raise BundleV3Error(
                "signed state resourceVersion differs from raw Terraform state"
            )
        if not all(
            isinstance(item[field], str) and item[field]
            for field in ("uid", "resource_version")
        ):
            raise BundleV3Error("signed state identity is incomplete")
        if not v2.SHA256_RE.fullmatch(str(item["object_sha256"])):
            raise BundleV3Error("signed state full-object digest is malformed")
        signed_by_address[address] = item
    if set(signed_by_address) != set(raw_by_address):
        raise BundleV3Error("signed state is not an exhaustive raw-state projection")

    entries: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    present_addresses: set[str] = set()
    for index, raw_entry in enumerate(objects):
        entry = exact(
            raw_entry,
            {"manifest", "live_identity", "state_address"},
            f"objects[{index}]",
        )
        identity = semantics.manifest_identity(entry["manifest"])
        if identity in entries:
            raise BundleV3Error("manifest identity is duplicated")
        entries[identity] = entry
        live_identity = exact(
            entry["live_identity"],
            {"present", "uid", "resource_version", "object_sha256"},
            f"objects[{index}].live_identity",
        )
        address = entry["state_address"]
        if live_identity["present"] is True:
            if not isinstance(address, str) or address not in raw_by_address:
                raise BundleV3Error("present manifest has no exact raw-state address")
            state_item = signed_by_address[address]
            if identity != (
                state_item["api_version"],
                state_item["kind"],
                state_item["namespace"],
                state_item["name"],
            ):
                raise BundleV3Error("manifest identity differs from its raw-state address")
            if (
                live_identity["uid"],
                live_identity["resource_version"],
                live_identity["object_sha256"],
            ) != (
                state_item["uid"],
                state_item["resource_version"],
                state_item["object_sha256"],
            ):
                raise BundleV3Error("manifest live identity differs from signed state")
            try:
                semantics.assert_manifest_matches_state(
                    entry["manifest"], raw_by_address[address]
                )
            except semantics.StateSemanticsError as error:
                raise BundleV3Error(str(error)) from error
            present_addresses.add(address)
        elif live_identity == {
            "present": False,
            "uid": None,
            "resource_version": None,
            "object_sha256": None,
        }:
            if address is not None:
                raise BundleV3Error("absent additive manifest claims a state address")
        else:
            raise BundleV3Error("absent manifest carries a substitutable live identity")
    if present_addresses != set(raw_by_address):
        raise BundleV3Error("manifests are not one-to-one with raw Terraform state")

    receipt_username = trust.get("receipt_username")
    if not isinstance(receipt_username, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9:._@/\-]{0,252}", receipt_username
    ):
        raise BundleV3Error("receipt username is not safe for an exact CEL identity")
    token_policy_identity = (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
        "",
        "fs2-pod-security-rollout-token-request",
    )
    token_policy = entries[token_policy_identity]["manifest"]
    validations = token_policy.get("spec", {}).get("validations", [])
    expected_receipt_expression = (
        f"request.userInfo.username == '{receipt_username}'"
    )
    if not isinstance(validations, list) or expected_receipt_expression not in {
        validation.get("expression")
        for validation in validations
        if isinstance(validation, dict)
    }:
        raise BundleV3Error(
            "TokenRequest admission is not bound to the exact current receipt identity"
        )

    token_identity = ("v1", "Secret", "fs2-system", "fs2-pod-security-token-anchor")
    token = entries[token_identity]
    if (
        token["manifest"].get("metadata", {})
        .get("annotations", {})
        .get("security.fs2.nebius.ai/custody-epoch-sha256")
        != trust["custody_epoch_sha256"]
        or token["state_address"] is not None
        or token["live_identity"]
        != {
            "present": False,
            "uid": None,
            "resource_version": None,
            "object_sha256": None,
        }
    ):
        raise BundleV3Error("token anchor is not an exact additive current-epoch create")
    return {
        **desired_result,
        "authoritative_state_objects_sha256": trust["state_objects_sha256"],
        "manifest_live_identity_sha256": state["objects_sha256"],
        "platform_state_object_count": str(len(raw_objects)),
        "platform_state_retained": "true",
        "raw_state_semantics_bound": "true",
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        required = {"bundle_path", "verified_trust_json"}
        if (
            not isinstance(query, dict)
            or set(query) != required
            or not all(isinstance(query[field], str) for field in required)
        ):
            raise BundleV3Error("external query fields differ from the v3 contract")
        trust = json.loads(query["verified_trust_json"])
        if not isinstance(trust, dict) or trust.get("valid") != "true":
            raise BundleV3Error("authoritative v3 trust result is absent or invalid")
        payload = v1.read_regular(Path(query["bundle_path"]), "manifest bundle")
        bundle = json.loads(payload)
        if not isinstance(bundle, dict) or v1.canonical(bundle) != payload:
            raise BundleV3Error("manifest bundle must be canonical JSON")
        key = v1.read_regular(Path(trust["authority_public_key_path"]), "manifest authority key", 65536)
        if hashlib.sha256(key).hexdigest() != trust["authority_public_key_sha256"]:
            raise BundleV3Error("manifest authority key differs after trust verification")
        v1.verify_signature(bundle, key, trust["authority_key_id"])
        result = {
            **validate(bundle, query, trust),
            "bundle_sha256": hashlib.sha256(payload).hexdigest(),
        }
    except (
        BundleV3Error,
        semantics.StateSemanticsError,
        v1.BundleError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"SAI-07 custody manifest v3 rejected: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
