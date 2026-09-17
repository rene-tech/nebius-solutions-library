#!/usr/bin/env python3
"""Verify exhaustive state adoption and refresh every live object before SSA.

The v1 verifier is retained as rejected-history-compatible desired-manifest
validation.  This v2 wrapper adds the controls that v1 lacked: repository-
pinned external trust, exact platform state lineage/serial/object inventory,
DaemonSet coverage, and immediate authenticated UID/resourceVersion/full-object
hash reads.  It never changes Kubernetes or Terraform state.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import verify_sai07_custody_manifest_bundle as v1

SCHEMA = "fs2-serve.nebius.ai/sai07-custody-manifest-bundle/v2"
STATE_SCHEMA = "fs2-serve.nebius.ai/sai07-platform-state-inventory/v2"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
DYNAMIC_ADDRESS_RE = re.compile(
    r'^(?:kubernetes_manifest\.pod_security_legacy_(?:networkpolicy|serviceaccount)_quarantine|'
    r'kubernetes_labels\.pod_security_legacy_daemonset_quarantine)\["[^"\\]+"\]$'
)

# Every static address formerly targeted by an active `removed` block is
# represented exactly once.  Dynamic retained-quarantine instances are matched
# separately and cannot be omitted from the signed state inventory.
STATIC_STATE: dict[str, tuple[str, str, str, str]] = {
    "kubernetes_manifest.pod_security_custody_boundary_policy": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-pod-security-custody-boundary"),
    "kubernetes_manifest.pod_security_custody_boundary_binding": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding", "", "fs2-pod-security-custody-boundary"),
    "kubernetes_manifest.node_observability_config_policy[0]": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-node-observability-configs"),
    "kubernetes_manifest.node_observability_config_binding[0]": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding", "", "fs2-node-observability-configs"),
    "kubernetes_manifest.node_observability_pod_policy[0]": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-node-observability-pods"),
    "kubernetes_manifest.node_observability_pod_binding[0]": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding", "", "fs2-node-observability-pods"),
    "kubernetes_manifest.node_observability_daemonset_policy[0]": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-node-observability-daemonsets"),
    "kubernetes_manifest.node_observability_daemonset_binding[0]": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding", "", "fs2-node-observability-daemonsets"),
    "kubernetes_service_account_v1.pod_security_rollout_manager": ("v1", "ServiceAccount", "fs2-system", "fs2-pod-security-rollout-manager"),
    "kubernetes_service_account_v1.pod_security_rollout_custodian": ("v1", "ServiceAccount", "fs2-system", "fs2-pod-security-rollout-custodian"),
    "kubernetes_service_account_v1.pod_security_metadata_reader": ("v1", "ServiceAccount", "fs2-system", "fs2-pod-security-metadata-reader"),
    "kubernetes_cluster_role_v1.pod_security_rollout_reader": ("rbac.authorization.k8s.io/v1", "ClusterRole", "", "fs2-pod-security-rollout-reader"),
    "kubernetes_cluster_role_binding_v1.pod_security_rollout_reader": ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding", "", "fs2-pod-security-rollout-reader"),
    "kubernetes_cluster_role_binding_v1.pod_security_rollout_custodian_reader": ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding", "", "fs2-pod-security-rollout-custodian-reader"),
    "kubernetes_role_v1.pod_security_rollout_ledger": ("rbac.authorization.k8s.io/v1", "Role", "fs2-system", "fs2-pod-security-rollout-ledger"),
    "kubernetes_role_binding_v1.pod_security_rollout_ledger": ("rbac.authorization.k8s.io/v1", "RoleBinding", "fs2-system", "fs2-pod-security-rollout-ledger"),
    "kubernetes_role_binding_v1.pod_security_rollout_custodian_ledger": ("rbac.authorization.k8s.io/v1", "RoleBinding", "fs2-system", "fs2-pod-security-rollout-custodian-ledger"),
    "kubernetes_role_v1.pod_security_secret_metadata_reader": ("rbac.authorization.k8s.io/v1", "Role", "fs2-models", "fs2-pod-security-secret-metadata-reader"),
    "kubernetes_role_binding_v1.pod_security_secret_metadata_reader": ("rbac.authorization.k8s.io/v1", "RoleBinding", "fs2-models", "fs2-pod-security-secret-metadata-reader"),
    "kubernetes_role_v1.pod_security_token_anchor_metadata_reader": ("rbac.authorization.k8s.io/v1", "Role", "fs2-system", "fs2-pod-security-token-anchor-metadata-reader"),
    "kubernetes_role_binding_v1.pod_security_token_anchor_metadata_reader": ("rbac.authorization.k8s.io/v1", "RoleBinding", "fs2-system", "fs2-pod-security-token-anchor-metadata-reader"),
    "kubernetes_role_v1.pod_security_metadata_reader_token_request": ("rbac.authorization.k8s.io/v1", "Role", "fs2-system", "fs2-pod-security-metadata-reader-token-request"),
    "kubernetes_role_binding_v1.pod_security_metadata_reader_token_request": ("rbac.authorization.k8s.io/v1", "RoleBinding", "fs2-system", "fs2-pod-security-metadata-reader-token-request"),
    "kubernetes_cluster_role_v1.pod_security_external_custody_audit": ("rbac.authorization.k8s.io/v1", "ClusterRole", "", "fs2-pod-security-external-custody-audit"),
    "kubernetes_cluster_role_binding_v1.pod_security_external_custody_audit": ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding", "", "fs2-pod-security-external-custody-audit"),
    "kubernetes_role_v1.pod_security_rollout_token_request": ("rbac.authorization.k8s.io/v1", "Role", "fs2-system", "fs2-pod-security-rollout-token-request"),
    "kubernetes_role_binding_v1.pod_security_rollout_token_request": ("rbac.authorization.k8s.io/v1", "RoleBinding", "fs2-system", "fs2-pod-security-rollout-token-request"),
    "kubernetes_manifest.pod_security_rollout_token_policy": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-pod-security-rollout-token-request"),
    "kubernetes_manifest.pod_security_rollout_token_binding": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding", "", "fs2-pod-security-rollout-token-request"),
    "kubernetes_manifest.pod_security_enforcement_fence_policy[0]": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-pod-security-enforcement-fence"),
    "kubernetes_manifest.pod_security_enforcement_fence_binding[0]": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding", "", "fs2-pod-security-enforcement-fence"),
    "kubernetes_manifest.pod_security_legacy_cleanup_fence_policy[0]": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-pod-security-legacy-cleanup-fence"),
    "kubernetes_manifest.pod_security_legacy_cleanup_fence_binding[0]": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding", "", "fs2-pod-security-legacy-cleanup-fence"),
    "kubernetes_manifest.pod_security_ledger_policy": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-pod-security-rollout-ledger"),
    "kubernetes_manifest.pod_security_ledger_binding": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding", "", "fs2-pod-security-rollout-ledger"),
    "kubernetes_manifest.snapshot_pod_policy[0]": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-snapshot-exact-profile"),
    "kubernetes_manifest.snapshot_pod_binding[0]": ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding", "", "fs2-snapshot-exact-profile"),
}

COUNTED_STATIC_STATE = frozenset(
    address for address in STATIC_STATE if address.endswith("[0]")
)
ADDITIVE_STATIC_STATE = frozenset(
    {
        "kubernetes_service_account_v1.pod_security_metadata_reader",
        "kubernetes_role_v1.pod_security_secret_metadata_reader",
        "kubernetes_role_binding_v1.pod_security_secret_metadata_reader",
        "kubernetes_role_v1.pod_security_token_anchor_metadata_reader",
        "kubernetes_role_binding_v1.pod_security_token_anchor_metadata_reader",
        "kubernetes_role_v1.pod_security_metadata_reader_token_request",
        "kubernetes_role_binding_v1.pod_security_metadata_reader_token_request",
    }
)
REQUIRED_STATIC_STATE = (
    frozenset(STATIC_STATE) - COUNTED_STATIC_STATE - ADDITIVE_STATIC_STATE
)

PLURALS = {
    ("v1", "ConfigMap"): "configmaps",
    ("v1", "ServiceAccount"): "serviceaccounts",
    ("apps/v1", "DaemonSet"): "daemonsets",
    ("rbac.authorization.k8s.io/v1", "Role"): "roles",
    ("rbac.authorization.k8s.io/v1", "RoleBinding"): "rolebindings",
    ("rbac.authorization.k8s.io/v1", "ClusterRole"): "clusterroles",
    ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"): "clusterrolebindings",
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy"): "validatingadmissionpolicies",
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding"): "validatingadmissionpolicybindings",
    ("networking.k8s.io/v1", "NetworkPolicy"): "networkpolicies",
}


class BundleV2Error(ValueError):
    pass


def exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BundleV2Error(f"{label} fields differ from the v2 contract")
    return value


def sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise BundleV2Error(f"{label} must be a lowercase SHA-256")
    return value


def api_path(identity: tuple[str, str, str, str]) -> str:
    api_version, kind, namespace, name = identity
    plural = PLURALS.get((api_version, kind))
    if plural is None:
        raise BundleV2Error(f"no exact live-read endpoint for {api_version}/{kind}")
    if api_version == "v1":
        prefix = "/api/v1"
    else:
        group, version = api_version.split("/", 1)
        prefix = f"/apis/{quote(group, safe='')}/{quote(version, safe='')}"
    if namespace:
        return f"{prefix}/namespaces/{quote(namespace, safe='')}/{plural}/{quote(name, safe='')}"
    return f"{prefix}/{plural}/{quote(name, safe='')}"


class LiveReader:
    def __init__(self, kubeconfig: str, context: str) -> None:
        self.base = ["kubectl", "--kubeconfig", kubeconfig, "--context", context]

    def raw(self, path: str, *, allow_absent: bool = False) -> dict[str, Any] | None:
        completed = subprocess.run(
            [*self.base, "get", "--raw", path],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if completed.returncode != 0:
            if allow_absent and b"not found" in completed.stderr.lower():
                return None
            raise BundleV2Error(f"authenticated live read failed for {path}")
        try:
            value = json.loads(completed.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BundleV2Error(f"live response for {path} is invalid") from error
        if not isinstance(value, dict):
            raise BundleV2Error(f"live response for {path} is not an object")
        return value


def validate(bundle: dict[str, Any], query: dict[str, str], trust: dict[str, str]) -> dict[str, str]:
    exact(
        bundle,
        {"schema", "cluster_id", "run_id", "kube_system_uid", "issued_at", "expires_at", "owner", "platform_exclusion", "iam_boundary_sha256", "objects_sha256", "objects", "platform_state", "signature"},
        "manifest bundle",
    )
    if bundle["schema"] != SCHEMA:
        raise BundleV2Error("manifest bundle schema is unsupported")
    if bundle["cluster_id"] != trust["cluster_id"] or bundle["kube_system_uid"] != trust["kube_system_uid"]:
        raise BundleV2Error("manifest bundle differs from external backend custody")
    objects = bundle["objects"]
    if not isinstance(objects, list) or not objects:
        raise BundleV2Error("manifest object list is empty")
    if hashlib.sha256(v1.canonical(objects)).hexdigest() != bundle["objects_sha256"]:
        raise BundleV2Error("manifest object aggregate differs")

    legacy_objects: list[dict[str, Any]] = []
    entries: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for index, raw in enumerate(objects):
        entry = exact(raw, {"manifest", "live_identity", "state_address"}, f"objects[{index}]")
        manifest = entry["manifest"]
        metadata = manifest.get("metadata", {}) if isinstance(manifest, dict) else {}
        identity = (manifest.get("apiVersion"), manifest.get("kind"), metadata.get("namespace", ""), metadata.get("name"))
        if not all(isinstance(part, str) and (part or position == 2) for position, part in enumerate(identity)) or identity in entries:
            raise BundleV2Error("manifest identity is incomplete or duplicated")
        entries[identity] = entry
        legacy_objects.append({"manifest": manifest, "live_identity": entry["live_identity"]})

    legacy_bundle = {key: value for key, value in bundle.items() if key != "platform_state"}
    legacy_bundle["schema"] = v1.SCHEMA
    legacy_bundle["objects"] = legacy_objects
    legacy_bundle["objects_sha256"] = hashlib.sha256(v1.canonical(legacy_objects)).hexdigest()
    owner_groups = json.loads(trust["owner_groups_json"])
    platform_groups = json.loads(trust["platform_groups_json"])
    if len(owner_groups) != 1 or len(platform_groups) != 1:
        raise BundleV2Error("v2 desired-manifest validator requires exact singleton identity groups")
    legacy_query = {
        "cluster_id": trust["cluster_id"],
        "kube_system_uid": trust["kube_system_uid"],
        "owner_username": trust["owner_username"],
        "owner_group": owner_groups[0],
        "platform_username": trust["platform_username"],
        "platform_group": platform_groups[0],
        "iam_boundary_sha256": trust["iam_receipt_sha256"],
    }
    legacy_result = v1.validate(legacy_bundle, legacy_query)

    state = exact(
        bundle["platform_state"],
        {"schema", "backend_receipt_sha256", "state_lineage", "state_serial", "state_object_version", "state_etag", "state_sha256", "objects_sha256", "objects"},
        "platform state",
    )
    if state["schema"] != STATE_SCHEMA:
        raise BundleV2Error("platform state inventory schema is unsupported")
    for field in ("backend_receipt_sha256", "state_sha256", "objects_sha256"):
        sha(state[field], f"platform_state.{field}")
    expected_state = {
        "backend_receipt_sha256": trust["backend_receipt_sha256"],
        "state_lineage": trust["state_lineage"],
        "state_serial": int(trust["state_serial"]),
        "state_object_version": trust["state_object_version"],
        "state_etag": trust["state_etag"],
        "state_sha256": trust["state_sha256"],
        "objects_sha256": trust["state_objects_sha256"],
    }
    for field, expected in expected_state.items():
        if state[field] != expected:
            raise BundleV2Error(f"platform state {field} differs from the backend receipt")
    state_objects = state["objects"]
    if not isinstance(state_objects, list) or hashlib.sha256(v1.canonical(state_objects)).hexdigest() != state["objects_sha256"]:
        raise BundleV2Error("platform state object inventory aggregate differs")
    by_address: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(state_objects):
        item = exact(raw, {"state_address", "api_version", "kind", "namespace", "name", "uid", "resource_version", "object_sha256"}, f"platform_state.objects[{index}]")
        address = item["state_address"]
        if not isinstance(address, str) or address in by_address:
            raise BundleV2Error("platform state address is invalid or duplicated")
        identity = (item["api_version"], item["kind"], item["namespace"], item["name"])
        if address in STATIC_STATE:
            if identity != STATIC_STATE[address]:
                raise BundleV2Error(f"static state identity differs at {address}")
        elif not DYNAMIC_ADDRESS_RE.fullmatch(address):
            raise BundleV2Error(f"unsupported platform state address {address}")
        for field in ("uid", "resource_version"):
            if not isinstance(item[field], str) or not item[field]:
                raise BundleV2Error(f"platform state {address} omits {field}")
        sha(item["object_sha256"], f"platform state {address} object_sha256")
        by_address[address] = item
    if not REQUIRED_STATIC_STATE.issubset(by_address):
        raise BundleV2Error("platform state inventory omits one or more unconditional custody addresses")
    if len(state_objects) != int(trust["state_object_count"]):
        raise BundleV2Error("platform state inventory count differs from backend attestation")

    present_addresses: set[str] = set()
    for identity, entry in entries.items():
        live = entry["live_identity"]
        address = entry["state_address"]
        if live["present"] is True:
            if not isinstance(address, str) or address not in by_address:
                raise BundleV2Error("a present object lacks its exact platform state address")
            state_item = by_address[address]
            if identity != (state_item["api_version"], state_item["kind"], state_item["namespace"], state_item["name"]):
                raise BundleV2Error("manifest identity differs from its platform state object")
            if (live["uid"], live["resource_version"], live["object_sha256"]) != (state_item["uid"], state_item["resource_version"], state_item["object_sha256"]):
                raise BundleV2Error("manifest live identity differs from exact platform state")
            present_addresses.add(address)
        elif address is not None:
            raise BundleV2Error("an absent additive object may not claim a platform state address")
    if present_addresses != set(by_address):
        raise BundleV2Error("adoption is not an exhaustive one-to-one projection of platform state")

    token_identity = (
        "v1",
        "Secret",
        "fs2-system",
        f"fs2-pod-security-token-anchor-v3-{trust['custody_epoch_sha256']}",
    )
    token_manifest = entries[token_identity]["manifest"]
    if (
        token_manifest.get("metadata", {})
        .get("annotations", {})
        .get("security.fs2.nebius.ai/custody-epoch-sha256")
        != trust["custody_epoch_sha256"]
    ):
        raise BundleV2Error("token anchor is not bound to the verified custody epoch")
    if entries[token_identity]["live_identity"] != {
        "present": False,
        "uid": None,
        "resource_version": None,
        "object_sha256": None,
    }:
        raise BundleV2Error("the immutable token anchor must be an additive create")

    reader = LiveReader(query["owner_kubeconfig_path"], query["owner_context"])
    kube_system = reader.raw("/api/v1/namespaces/kube-system")
    if kube_system is None or kube_system.get("metadata", {}).get("uid") != trust["kube_system_uid"]:
        raise BundleV2Error("immediate live read selected another cluster")
    refreshed: list[dict[str, Any]] = []
    manifests = json.loads(legacy_result["manifests_json"])
    for identity, entry in sorted(entries.items()):
        if identity == token_identity:
            continue
        live = reader.raw(api_path(identity), allow_absent=entry["live_identity"]["present"] is False)
        if entry["live_identity"]["present"] is False:
            if live is not None:
                raise BundleV2Error(f"object claimed absent now exists: {'/'.join(identity)}")
            continue
        if live is None:
            raise BundleV2Error(f"object disappeared before SSA: {'/'.join(identity)}")
        metadata = live.get("metadata", {})
        observed = {
            "uid": metadata.get("uid"),
            "resource_version": metadata.get("resourceVersion"),
            "object_sha256": hashlib.sha256(v1.canonical(live)).hexdigest(),
        }
        expected = entry["live_identity"]
        if observed != {"uid": expected["uid"], "resource_version": expected["resource_version"], "object_sha256": expected["object_sha256"]}:
            raise BundleV2Error(f"live object changed before SSA: {'/'.join(identity)}")
        key = "/".join(identity)
        manifests[key]["metadata"]["uid"] = expected["uid"]
        manifests[key]["metadata"]["resourceVersion"] = expected["resource_version"]
        refreshed.append({"identity": key, **observed})
    return {
        **legacy_result,
        "manifests_json": json.dumps(manifests, sort_keys=True, separators=(",", ":")),
        "platform_state_objects_sha256": state["objects_sha256"],
        "platform_state_object_count": str(len(state_objects)),
        "refreshed_live_objects_sha256": hashlib.sha256(v1.canonical(refreshed)).hexdigest(),
        "refreshed_live_object_count": str(len(refreshed)),
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        required = {"bundle_path", "verified_trust_json", "owner_kubeconfig_path", "owner_context"}
        if not isinstance(query, dict) or set(query) != required or not all(isinstance(query[key], str) for key in required):
            raise BundleV2Error("external query fields differ from the v2 contract")
        trust = json.loads(query["verified_trust_json"])
        if not isinstance(trust, dict) or trust.get("valid") != "true":
            raise BundleV2Error("external trust result is absent or invalid")
        payload = v1.read_regular(Path(query["bundle_path"]), "manifest bundle")
        bundle = json.loads(payload)
        if not isinstance(bundle, dict) or v1.canonical(bundle) != payload:
            raise BundleV2Error("manifest bundle must be canonical JSON")
        key = v1.read_regular(Path(trust["authority_public_key_path"]), "authority public key", 65536)
        if hashlib.sha256(key).hexdigest() != trust["authority_public_key_sha256"]:
            raise BundleV2Error("authority key differs after trust verification")
        v1.verify_signature(bundle, key, trust["authority_key_id"])
        result = validate(bundle, query, trust)
    except (BundleV2Error, v1.BundleError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"SAI-07 custody manifest v2 rejected: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
