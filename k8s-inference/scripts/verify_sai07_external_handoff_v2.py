#!/usr/bin/env python3
"""Verify an exact externally owned SAI-07 adoption/authority handoff.

This successor rejects count-only adoption and digest-only authorization
summaries.  It pins the signing authority in the repository trust lock, binds
all five authenticated actors, validates the full SSAR/SSRR evidence, and
requires a one-to-one pre/post adoption receipt for the complete platform state
inventory.  The checked-in inactive trust lock makes this path fail closed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import audit_sai07_effective_authority_v2 as authority
import verify_sai07_custody_manifest_bundle as manifest_v1
import verify_sai07_custody_manifest_bundle_v2 as manifest_v2
import verify_sai07_external_handoff as handoff_v1

SCHEMA = "fs2-serve.nebius.ai/sai07-external-custody-handoff/v2"
ADOPTION_SCHEMA = "fs2-serve.nebius.ai/sai07-custody-adoption/v2"
TRUST_SCHEMA = "fs2-serve.nebius.ai/sai07-external-trust-attestation/v2"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class HandoffV2Error(ValueError):
    pass


def exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise HandoffV2Error(f"{label} fields differ from the v2 contract")
    return value


def sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise HandoffV2Error(f"{label} must be a lowercase SHA-256")
    return value


def validate_full_rules(profile: str, namespaces: list[str], persistent_volume_names: list[str], rules: list[dict[str, Any]]) -> None:
    """Reject any SSRR-sensitive grant outside the exact SSAR allow matrix."""
    expected = authority.expected_reviews(profile, namespaces, persistent_volume_names)
    cluster_resources = {
        (group, resource, subresource)
        for group, resource, subresource in authority.CLUSTER_RESOURCES
    }
    namespaced_resources = {
        (group, resource, subresource)
        for group, resource, subresource in authority.NAMESPACED_RESOURCES
    }
    for namespace_entry in rules:
        namespace = namespace_entry["namespace"]
        status = namespace_entry.get("status")
        if not isinstance(status, dict) or status.get("incomplete") is not False:
            raise HandoffV2Error(f"authority audit {profile} has an incomplete SSRR")
        for rule in status.get("resourceRules", []):
            if not isinstance(rule, dict):
                raise HandoffV2Error(f"authority audit {profile} has a malformed resource rule")
            verbs = rule.get("verbs", [])
            groups = rule.get("apiGroups", [])
            resources = rule.get("resources", [])
            names = rule.get("resourceNames", [])
            if not all(isinstance(values, list) for values in (verbs, groups, resources, names)):
                raise HandoffV2Error(f"authority audit {profile} has malformed rule vectors")
            if "*" in verbs or "*" in groups or "*" in resources:
                raise HandoffV2Error(f"authority audit {profile} retains wildcard authority")
            for verb in verbs:
                for group in groups:
                    for combined in resources:
                        resource, separator, subresource = combined.partition("/")
                        identity = (group, resource, subresource if separator else "")
                        if identity in cluster_resources:
                            scope = ""
                        elif identity in namespaced_resources:
                            scope = namespace
                        else:
                            if verb in {*authority.MUTATING, "bind", "escalate", "impersonate"}:
                                raise HandoffV2Error(
                                    f"authority audit {profile} retains an unreviewed mutating resource edge"
                                )
                            continue
                        candidate_names = names or [""]
                        for name in candidate_names:
                            identifier = authority.check_id(
                                verb, group, resource, subresource if separator else "", scope, name
                            )
                            if expected.get(identifier) is not True:
                                raise HandoffV2Error(
                                    f"authority audit {profile} SSRR grants an unexpected sensitive edge"
                                )
        for rule in status.get("nonResourceRules", []):
            if not isinstance(rule, dict):
                raise HandoffV2Error(f"authority audit {profile} has a malformed non-resource rule")
            verbs = rule.get("verbs", [])
            urls = rule.get("nonResourceURLs", [])
            if (
                not isinstance(verbs, list)
                or not isinstance(urls, list)
                or any(verb not in {"get"} for verb in verbs)
                or any(
                    not isinstance(url, str)
                    or not url.startswith(("/api", "/apis", "/openapi", "/version", "/healthz", "/livez", "/readyz"))
                    for url in urls
                )
            ):
                raise HandoffV2Error(f"authority audit {profile} retains a non-discovery URL edge")


def load_lock(path: Path) -> tuple[dict[str, Any], bytes]:
    payload = manifest_v1.read_regular(path, "custody trust lock")
    value = json.loads(payload)
    if not isinstance(value, dict) or manifest_v1.canonical(value) != payload:
        raise HandoffV2Error("custody trust lock must be canonical JSON")
    exact(value, {"schema", "activation", "authority_public_key_path", "authority_public_key_sha256", "authority_key_id", "backend", "expected"}, "custody trust lock")
    if value["schema"] != "fs2-serve.nebius.ai/sai07-custody-trust-lock/v2" or value["activation"] != "active":
        raise HandoffV2Error("repository-pinned custody trust is not active")
    key = manifest_v1.read_regular(Path(value["authority_public_key_path"]), "custody authority public key", 65536)
    if hashlib.sha256(key).hexdigest() != value["authority_public_key_sha256"]:
        raise HandoffV2Error("custody authority key differs from the trust lock")
    return value, key


def validate_audit(raw: object, profile: str, username: str, groups: list[str], jti: str | None, cluster_id: str, kube_system_uid: str) -> dict[str, Any]:
    audit = exact(
        raw,
        {
            "schema", "matrix_version", "profile", "username", "groups",
            "credential_jti_sha256", "cluster_id", "kube_system_uid",
            "namespace_inventory", "namespace_inventory_sha256",
            "persistent_volume_names",
            "self_subject_rules_reviews", "self_subject_rules_reviews_sha256",
            "self_subject_access_reviews", "self_subject_access_reviews_sha256",
            "observed_at",
        },
        f"authority audit {profile}",
    )
    if audit["schema"] != authority.SCHEMA or audit["matrix_version"] != authority.MATRIX_VERSION or audit["profile"] != profile:
        raise HandoffV2Error(f"authority audit {profile} schema/profile differs")
    if audit["username"] != username or audit["groups"] != groups or audit["credential_jti_sha256"] != jti:
        raise HandoffV2Error(f"authority audit {profile} is not bound to the claimed credential")
    if audit["cluster_id"] != cluster_id or audit["kube_system_uid"] != kube_system_uid:
        raise HandoffV2Error(f"authority audit {profile} belongs to another cluster")
    namespaces = audit["namespace_inventory"]
    if not isinstance(namespaces, list) or not namespaces or namespaces != sorted(set(namespaces)):
        raise HandoffV2Error(f"authority audit {profile} namespace inventory is incomplete")
    if hashlib.sha256(manifest_v1.canonical(namespaces)).hexdigest() != audit["namespace_inventory_sha256"]:
        raise HandoffV2Error(f"authority audit {profile} namespace aggregate differs")
    persistent_volume_names = audit["persistent_volume_names"]
    if (
        not isinstance(persistent_volume_names, list)
        or persistent_volume_names != sorted(set(persistent_volume_names))
        or len(persistent_volume_names) != 8
        or not authority.FIXED_PERSISTENT_VOLUMES.issubset(persistent_volume_names)
    ):
        raise HandoffV2Error(f"authority audit {profile} persistent-volume inventory differs")
    rules = audit["self_subject_rules_reviews"]
    reviews = audit["self_subject_access_reviews"]
    if hashlib.sha256(manifest_v1.canonical(rules)).hexdigest() != audit["self_subject_rules_reviews_sha256"]:
        raise HandoffV2Error(f"authority audit {profile} full SSRR evidence differs")
    if hashlib.sha256(manifest_v1.canonical(reviews)).hexdigest() != audit["self_subject_access_reviews_sha256"]:
        raise HandoffV2Error(f"authority audit {profile} full SSAR evidence differs")
    if not isinstance(rules, list) or [item.get("namespace") for item in rules if isinstance(item, dict)] != namespaces:
        raise HandoffV2Error(f"authority audit {profile} does not contain one SSRR per namespace")
    validate_full_rules(profile, namespaces, persistent_volume_names, rules)
    expected = authority.expected_reviews(profile, namespaces, persistent_volume_names)
    observed: dict[str, bool] = {}
    for item in reviews if isinstance(reviews, list) else []:
        entry = exact(item, {"check", "allowed", "expected_allowed"}, f"authority audit {profile} review")
        if entry["check"] in observed or entry["expected_allowed"] is not expected.get(entry["check"]):
            raise HandoffV2Error(f"authority audit {profile} review matrix differs")
        if entry["allowed"] is not entry["expected_allowed"]:
            raise HandoffV2Error(f"authority audit {profile} has unexpected effective authority")
        observed[entry["check"]] = entry["allowed"]
    if set(observed) != set(expected):
        raise HandoffV2Error(f"authority audit {profile} omits a required RBAC pivot check")
    observed_at = handoff_v1.instant(audit["observed_at"], f"authority audit {profile}.observed_at")
    if dt.datetime.now(dt.UTC) - observed_at > handoff_v1.MAX_HANDOFF_AGE + handoff_v1.MAX_CLOCK_SKEW:
        raise HandoffV2Error(f"authority audit {profile} is stale")
    return audit


def validate_adoption(value: object, trust: dict[str, Any]) -> dict[str, Any]:
    adoption = exact(
        value,
        {"schema", "backend_receipt_sha256", "state_lineage", "state_serial", "state_object_version", "state_etag", "state_sha256", "pre_objects_sha256", "post_objects_sha256", "object_count", "objects"},
        "adopted objects",
    )
    if adoption["schema"] != ADOPTION_SCHEMA:
        raise HandoffV2Error("adoption schema is unsupported")
    for field in ("backend_receipt_sha256", "state_sha256", "pre_objects_sha256", "post_objects_sha256"):
        sha(adoption[field], f"adoption.{field}")
    comparisons = {
        "backend_receipt_sha256": trust["backend_receipt_sha256"],
        "state_lineage": trust["state_lineage"],
        "state_serial": trust["state_serial"],
        "state_object_version": trust["state_object_version"],
        "state_etag": trust["state_etag"],
        "state_sha256": trust["state_sha256"],
        "pre_objects_sha256": trust["state_objects_sha256"],
    }
    for field, expected in comparisons.items():
        if adoption[field] != expected:
            raise HandoffV2Error(f"adoption {field} differs from externally attested state")
    objects = adoption["objects"]
    if not isinstance(objects, list) or adoption["object_count"] != len(objects) or not objects:
        raise HandoffV2Error("adoption object set is empty or count-inconsistent")
    pre: list[dict[str, Any]] = []
    post: list[dict[str, Any]] = []
    addresses: set[str] = set()
    for index, raw in enumerate(objects):
        entry = exact(raw, {"state_object", "post_live_identity"}, f"adoption.objects[{index}]")
        state_object = exact(entry["state_object"], {"state_address", "api_version", "kind", "namespace", "name", "uid", "resource_version", "object_sha256"}, f"adoption.objects[{index}].state_object")
        post_live = exact(entry["post_live_identity"], {"uid", "resource_version", "object_sha256"}, f"adoption.objects[{index}].post_live_identity")
        address = state_object["state_address"]
        if address in addresses:
            raise HandoffV2Error("adoption state address is duplicated")
        identity = (state_object["api_version"], state_object["kind"], state_object["namespace"], state_object["name"])
        if address in manifest_v2.STATIC_STATE:
            if identity != manifest_v2.STATIC_STATE[address]:
                raise HandoffV2Error("adoption static address identity differs")
        elif not manifest_v2.DYNAMIC_ADDRESS_RE.fullmatch(address):
            raise HandoffV2Error("adoption includes an unsupported state address")
        if post_live["uid"] != state_object["uid"]:
            raise HandoffV2Error("SSA replaced an adopted object UID")
        for field in ("object_sha256",):
            sha(state_object[field], f"adoption pre {field}")
            sha(post_live[field], f"adoption post {field}")
        if not all(isinstance(post_live[field], str) and post_live[field] for field in ("uid", "resource_version")):
            raise HandoffV2Error("post-SSA live identity is incomplete")
        addresses.add(address)
        pre.append(state_object)
        post.append({"state_address": address, **post_live})
    if not set(manifest_v2.STATIC_STATE).issubset(addresses):
        raise HandoffV2Error("adoption omits a static platform-owned custody address")
    if hashlib.sha256(manifest_v1.canonical(pre)).hexdigest() != adoption["pre_objects_sha256"]:
        raise HandoffV2Error("adoption pre-object aggregate differs")
    if hashlib.sha256(manifest_v1.canonical(post)).hexdigest() != adoption["post_objects_sha256"]:
        raise HandoffV2Error("adoption post-object aggregate differs")
    return adoption


def validate(bundle: dict[str, Any], query: dict[str, str], lock: dict[str, Any]) -> dict[str, str]:
    exact(
        bundle,
        {
            "schema", "cluster_id", "run_id", "kube_system_uid", "context_sha256",
            "phase", "consumer", "action", "bundle_sha256", "issued_at", "expires_at",
            "nonce", "custody", "external_trust", "receipt_token_request",
            "metadata_token_request", "authority_audits", "secret_metadata_inventory",
            "token_anchor_metadata", "adopted_objects", "ledger", "signature",
        },
        "handoff",
    )
    if bundle["schema"] != SCHEMA:
        raise HandoffV2Error("handoff schema is unsupported")
    expected = lock["expected"]
    if bundle["cluster_id"] != expected["cluster_id"] or bundle["kube_system_uid"] != expected["kube_system_uid"]:
        raise HandoffV2Error("handoff cluster differs from the trust lock")
    trust = exact(
        bundle["external_trust"],
        {"schema", "authority_key_id", "authority_public_key_sha256", "iam_receipt_sha256", "backend_receipt_sha256", "provider_tenant_id", "provider_project_id", "backend_bucket", "backend_key", "backend_region", "state_lineage", "state_serial", "state_object_version", "state_etag", "state_sha256", "state_objects_sha256", "state_object_count", "persistent_volume_names"},
        "external trust",
    )
    if trust["schema"] != TRUST_SCHEMA:
        raise HandoffV2Error("external trust schema is unsupported")
    lock_values = {
        "authority_key_id": lock["authority_key_id"],
        "authority_public_key_sha256": lock["authority_public_key_sha256"],
        "provider_tenant_id": expected["provider_tenant_id"],
        "provider_project_id": expected["provider_project_id"],
        "backend_bucket": lock["backend"]["bucket"],
        "backend_key": lock["backend"]["key"],
        "backend_region": lock["backend"]["region"],
    }
    for field, value in lock_values.items():
        if trust[field] != value:
            raise HandoffV2Error(f"external trust {field} differs from the repository lock")
    for field in ("authority_public_key_sha256", "iam_receipt_sha256", "backend_receipt_sha256", "state_sha256", "state_objects_sha256"):
        sha(trust[field], f"external_trust.{field}")
    if not isinstance(trust["state_object_count"], int) or isinstance(trust["state_object_count"], bool) or trust["state_object_count"] < len(manifest_v2.STATIC_STATE):
        raise HandoffV2Error("external trust state object count omits static custody addresses")
    if (
        not isinstance(trust["persistent_volume_names"], list)
        or trust["persistent_volume_names"] != sorted(set(trust["persistent_volume_names"]))
        or len(trust["persistent_volume_names"]) != 8
        or not authority.FIXED_PERSISTENT_VOLUMES.issubset(trust["persistent_volume_names"])
    ):
        raise HandoffV2Error("external trust persistent-volume inventory is incomplete")

    receipt_token = handoff_v1.validate_bound_token(bundle["receipt_token_request"], "receipt_token_request", "fs2-pod-security-rollout-custodian")
    metadata_token = handoff_v1.validate_bound_token(bundle["metadata_token_request"], "metadata_token_request", "fs2-pod-security-metadata-reader")
    anchor_metadata = exact(
        bundle["token_anchor_metadata"],
        {"schema", "media_type", "namespace", "collection_resource_version", "observed_at", "items", "items_sha256", "contains_secret_payload", "reader_service_account_uid", "token_jti_sha256", "token_bound_object_ref"},
        "token anchor metadata",
    )
    if (
        anchor_metadata["schema"] != "fs2-serve.nebius.ai/sai07-token-anchor-metadata/v1"
        or anchor_metadata["media_type"] != handoff_v1.PARTIAL_METADATA_ACCEPT
        or anchor_metadata["namespace"] != "fs2-system"
        or anchor_metadata["contains_secret_payload"] is not False
        or anchor_metadata["reader_service_account_uid"] != metadata_token["service_account"]["uid"]
        or anchor_metadata["token_jti_sha256"] != metadata_token["jti_sha256"]
        or anchor_metadata["token_bound_object_ref"] != metadata_token["bound_object_ref"]
    ):
        raise HandoffV2Error("token anchor metadata is not bound to the exact metadata-reader credential")
    anchor_items = anchor_metadata["items"]
    anchor_observed_at = handoff_v1.instant(
        anchor_metadata["observed_at"], "token_anchor_metadata.observed_at"
    )
    if (
        not isinstance(anchor_items, list)
        or len(anchor_items) != 1
        or hashlib.sha256(manifest_v1.canonical(anchor_items)).hexdigest() != anchor_metadata["items_sha256"]
        or anchor_items[0]
        != {
            "namespace": "fs2-system",
            "name": "fs2-pod-security-token-anchor",
            "uid": metadata_token["bound_object_ref"]["uid"],
            "resource_version": anchor_items[0].get("resource_version"),
        }
        or not isinstance(anchor_items[0].get("resource_version"), str)
        or not anchor_items[0]["resource_version"]
        or not isinstance(anchor_metadata["collection_resource_version"], str)
        or not anchor_metadata["collection_resource_version"]
        or not SHA256_RE.fullmatch(str(anchor_metadata["items_sha256"]))
        or dt.datetime.now(dt.UTC) - anchor_observed_at
        > handoff_v1.MAX_HANDOFF_AGE + handoff_v1.MAX_CLOCK_SKEW
    ):
        raise HandoffV2Error("token anchor metadata does not prove the exact post-create anchor")
    audits = exact(bundle["authority_audits"], set(authority.PROFILES), "authority audits")
    service_groups = ["system:authenticated", "system:serviceaccounts", "system:serviceaccounts:fs2-system"]
    claims = {
        "platform": (expected["platform_username"], expected["platform_groups"], None),
        "inactive-owner": (expected["owner_username"], expected["owner_groups"], None),
        "token-issuer": (expected["receipt_username"], expected["receipt_groups"], None),
        "receipt-service-account": ("system:serviceaccount:fs2-system:fs2-pod-security-rollout-custodian", service_groups, receipt_token["jti_sha256"]),
        "metadata-reader": ("system:serviceaccount:fs2-system:fs2-pod-security-metadata-reader", service_groups, metadata_token["jti_sha256"]),
    }
    validated = {
        profile: validate_audit(audits[profile], profile, *claims[profile], bundle["cluster_id"], bundle["kube_system_uid"])
        for profile in sorted(authority.PROFILES)
    }
    inventories = {(audit["namespace_inventory_sha256"], tuple(audit["namespace_inventory"])) for audit in validated.values()}
    if len(inventories) != 1:
        raise HandoffV2Error("authority audits do not cover one exact namespace inventory")
    if any(audit["persistent_volume_names"] != trust["persistent_volume_names"] for audit in validated.values()):
        raise HandoffV2Error("authority audits do not bind the externally attested PV inventory")

    adoption = validate_adoption(bundle["adopted_objects"], trust)
    if adoption["object_count"] != trust["state_object_count"]:
        raise HandoffV2Error("adoption count differs from backend-attested exact inventory")
    custody = bundle["custody"]
    if custody.get("owner_username") != expected["owner_username"] or custody.get("owner_groups") != expected["owner_groups"] or custody.get("receipt_username") != expected["receipt_username"] or custody.get("receipt_groups") != expected["receipt_groups"] or custody.get("iam_boundary_sha256") != trust["iam_receipt_sha256"]:
        raise HandoffV2Error("handoff custody claims differ from provider-attested identities")

    # Reuse the retained semantic checks for tokens, metadata, ledger and phase
    # after projecting the v2 full-evidence fields into its v1 shape.
    def audit_summary(audit: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "fs2-serve.nebius.ai/sai07-effective-authority-audit/v1",
            "username": audit["username"],
            "groups": audit["groups"],
            "cluster_id": audit["cluster_id"],
            "kube_system_uid": audit["kube_system_uid"],
            "namespace_inventory_sha256": audit["namespace_inventory_sha256"],
            "namespace_count": len(audit["namespace_inventory"]),
            "self_subject_rules_review_sha256": audit["self_subject_rules_reviews_sha256"],
            "self_subject_access_review_sha256": audit["self_subject_access_reviews_sha256"],
            "denied_checks": sorted(handoff_v1.REQUIRED_DENIALS),
            "observed_at": audit["observed_at"],
        }
    projected = {key: value for key, value in bundle.items() if key not in {"external_trust", "authority_audits", "token_anchor_metadata"}}
    projected["schema"] = handoff_v1.SCHEMA
    projected["platform_authority_audit"] = audit_summary(validated["platform"])
    projected["owner_authority_audit"] = audit_summary(validated["inactive-owner"])
    projected["adopted_objects"] = {"schema": "fs2-serve.nebius.ai/sai07-custody-adoption/v1", "artifact_sha256": adoption["post_objects_sha256"], "count": adoption["object_count"]}
    result = handoff_v1.validate(projected, query)
    return {**result, "adopted_state_objects_sha256": adoption["pre_objects_sha256"], "adopted_post_objects_sha256": adoption["post_objects_sha256"], "authority_matrix_version": authority.MATRIX_VERSION}


def main() -> int:
    try:
        query = json.load(sys.stdin)
        required = {"handoff_path", "trust_lock_path", "receipt_bundle_sha256", "expected_context_sha256", "expected_phase", "expected_consumer", "expected_action", "cluster_id", "kube_system_uid"}
        if not isinstance(query, dict) or set(query) != required or not all(isinstance(query[key], str) for key in required):
            raise HandoffV2Error("external query fields differ from the v2 contract")
        lock, key = load_lock(Path(query["trust_lock_path"]))
        payload = manifest_v1.read_regular(Path(query["handoff_path"]), "external custody handoff")
        bundle = json.loads(payload)
        if not isinstance(bundle, dict) or manifest_v1.canonical(bundle) != payload:
            raise HandoffV2Error("external custody handoff must be canonical JSON")
        manifest_v1.verify_signature(bundle, key, lock["authority_key_id"])
        result = validate(bundle, query, lock)
    except (HandoffV2Error, handoff_v1.HandoffError, manifest_v1.BundleError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"SAI-07 external handoff v2 rejected: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
