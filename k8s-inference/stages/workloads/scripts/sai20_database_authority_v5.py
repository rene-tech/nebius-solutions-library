#!/usr/bin/env python3
"""Fail-closed successor gate for the rejected SAI-20 v4 authority path.

Rejected v4 commits remain immutable Git evidence.  This wrapper first runs
the inherited v4 checks, then adds the independent root-enrollment chain, complete
cluster-binding derivation, CNPG peer custody, pre-existing bootstrap guard and
apply-time provider-group re-observation required by the v5 successor.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import sai20_database_authority as v3
import sai20_database_authority_v4 as v4


SCHEMA = "fs2-serve.nebius.ai/sai20-database-authority/v5"
ENROLLMENT_AUTHORITIES_SCHEMA = "fs2-serve.nebius.ai/sai20-enrollment-authorities/v1"
ENROLLMENT_RECEIPTS_SCHEMA = "fs2-serve.nebius.ai/sai20-root-enrollment-receipts/v1"
REJECTED_COMMITS = {
    *v4.REJECTED_COMMITS,
    "d5c19b3a8b3345acbec7b16bd5a2c00a455874d8",
    "efb29e684e0c91b06553d76b43c487a8531016f2",
    "a51b1d80738a66774eaef945c6870ba79549a816",
    "948e1836b4058779aff2c0c91c62aa898968da5d",
    "17469ed79eb56ae63327f0ddecb81d21b2170722",
}
ROLLOUT_LINEAGE_LABEL = "security.fs2.nebius.ai/sai20-rollout-lineage"
PEER_NAMESPACES = ("fs2-data", "cnpg-system")
PEER_RESOURCES = tuple(sorted(v4.WORKLOAD_TYPES))
PEER_ENDPOINTS = {
    (namespace, resource): (
        f"/api/v1/namespaces/{namespace}/{resource}"
        if resource in {"pods", "replicationcontrollers"}
        else f"/apis/apps/v1/namespaces/{namespace}/{resource}"
        if resource in {"deployments", "statefulsets", "daemonsets", "replicasets"}
        else f"/apis/batch/v1/namespaces/{namespace}/{resource}"
    )
    for namespace in PEER_NAMESPACES
    for resource in PEER_RESOURCES
}
CNPG_CLUSTER_ENDPOINT = "/apis/postgresql.cnpg.io/v1/namespaces/fs2-data/clusters/fs2-control-db"
ADMISSION_POLICY_ENDPOINT = "/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicies"
ADMISSION_BINDING_ENDPOINT = "/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicybindings"
SUPPLEMENTAL_ENDPOINTS = {
    **{
        f"k8s/peer-workloads/{namespace}/{resource}": endpoint
        for (namespace, resource), endpoint in PEER_ENDPOINTS.items()
    },
    "k8s/cnpg-cluster/fs2-data/fs2-control-db": CNPG_CLUSTER_ENDPOINT,
    "k8s/admission/validatingadmissionpolicies": ADMISSION_POLICY_ENDPOINT,
    "k8s/admission/validatingadmissionpolicybindings": ADMISSION_BINDING_ENDPOINT,
}
TRANSITION_ADMISSION_NAMES = {
    "fs2-database-authority-object-custody-v4",
    "fs2-database-authority-object-custody-binding-v4",
    "fs2-database-policy-freeze-v4",
    "fs2-database-policy-freeze-binding-v4",
    "fs2-database-exact-owner-v4",
    "fs2-database-exact-owner-binding-v4",
    "fs2-database-peer-identity-v5",
    "fs2-database-peer-identity-binding-v5",
}
SUCCESSOR_SOURCE_PATHS = {
    "k8s-inference/security/sai20/enrollment-authorities-v1.json",
    "k8s-inference/security/sai20/root-enrollment-receipts-v1.json",
    "k8s-inference/stages/workloads/contracts/sai20-bootstrap-guard-v5.json",
    "k8s-inference/stages/workloads/sai20_database_authority_v5.tf",
    "k8s-inference/stages/workloads/scripts/sai20_database_authority_v5.py",
    "k8s-inference/tests/test_sai20_database_authority_v5.py",
}


def require(condition: bool, message: str) -> None:
    v4.require(condition, message)


def exact_keys(value: dict[str, Any], keys: set[str], where: str) -> None:
    v4.exact_keys(value, keys, where)


def text(value: Any, where: str) -> str:
    return v4.text(value, where)


def digest(value: Any) -> str:
    return v4.digest(value)


def sha256(value: Any, where: str) -> str:
    return v4.sha256(value, where)


def oid(value: Any, where: str) -> str:
    return v4.oid(value, where)


def source_json(query: dict[str, str], path_key: str, hash_key: str, where: str) -> dict[str, Any]:
    raw = v3.safe_read(query[path_key], path_key, 4 * 1024 * 1024)
    require(hashlib.sha256(raw).hexdigest() == query[hash_key], f"{where} bytes differ from source")
    value = v4.parse_json_bytes(raw, where)
    require(isinstance(value, dict), f"{where} must be an object")
    return value


def verify_external_root_enrollment(
    query: dict[str, str],
    roots: dict[str, dict[str, Any]],
    repository: Path,
) -> str:
    current_authorities = source_json(
        query,
        "enrollment_authorities_path",
        "expected_enrollment_authorities_sha256",
        "enrollment authority registry",
    )
    exact_keys(
        current_authorities,
        {"schema", "status", "authorities", "requirements"},
        "enrollment authority registry",
    )
    require(current_authorities["schema"] == ENROLLMENT_AUTHORITIES_SCHEMA, "enrollment authority schema mismatch")
    require(current_authorities["status"] == "ACTIVE", "external enrollment authority registry is not ACTIVE")
    require(isinstance(current_authorities["authorities"], list) and current_authorities["authorities"], "no external enrollment authority is established")

    receipts_document = source_json(
        query,
        "root_enrollment_receipts_path",
        "expected_root_enrollment_receipts_sha256",
        "root enrollment receipts",
    )
    exact_keys(receipts_document, {"schema", "status", "receipts", "note"}, "root enrollment receipts")
    require(receipts_document["schema"] == ENROLLMENT_RECEIPTS_SCHEMA, "root enrollment receipt schema mismatch")
    require(receipts_document["status"] == "ACTIVE", "root enrollment receipts are not ACTIVE")
    receipts = receipts_document["receipts"]
    require(isinstance(receipts, list) and len(receipts) == len(roots), "every evidence root needs one external enrollment receipt")

    accepted: set[str] = set()
    for index, receipt in enumerate(receipts):
        where = f"root enrollment receipts[{index}]"
        require(isinstance(receipt, dict), f"{where} must be an object")
        exact_keys(receipt, {"statement", "authority_key_id", "signature"}, where)
        statement = receipt["statement"]
        require(isinstance(statement, dict), f"{where}.statement must be an object")
        exact_keys(
            statement,
            {
                "purpose", "root_key_id", "root_identity_sha256", "root_role",
                "root_principal_id", "root_group_id", "accepted_source_commit",
                "accepted_source_tree", "authority_snapshot", "verdict", "accepted_at",
            },
            f"{where}.statement",
        )
        root_key_id = text(statement["root_key_id"], f"{where}.statement.root_key_id")
        require(root_key_id in roots and root_key_id not in accepted, f"{where} names an unknown or duplicate root")
        root = roots[root_key_id]
        root_identity = {
            "key_id": root_key_id,
            "public_key": root["public_key"],
            "role": root["role"],
            "principal_id": root["principal_id"],
            "group_id": root["group_id"],
            "provenance": root["provenance"],
        }
        require(statement["purpose"] == "sai20-root-enrollment", f"{where} purpose mismatch")
        require(statement["root_identity_sha256"] == digest(root_identity), f"{where} root identity is not content-derived")
        require(statement["root_role"] == root["role"], f"{where} role mismatch")
        require(statement["root_principal_id"] == root["principal_id"], f"{where} principal mismatch")
        require(statement["root_group_id"] == root["group_id"], f"{where} group mismatch")
        provenance = root["provenance"]
        root_commit = oid(provenance["accepted_source_commit"], f"{where} accepted root commit")
        require(statement["accepted_source_commit"] == root_commit, f"{where} accepted source commit mismatch")
        require(statement["accepted_source_tree"] == provenance["accepted_source_tree"], f"{where} accepted source tree mismatch")
        require(statement["verdict"] == "ACCEPTED", f"{where} verdict is not ACCEPTED")
        v3.parse_time(statement["accepted_at"], f"{where}.statement.accepted_at")

        snapshot = statement["authority_snapshot"]
        require(isinstance(snapshot, dict), f"{where}.authority_snapshot must be an object")
        exact_keys(snapshot, {"commit", "tree", "path", "blob"}, f"{where}.authority_snapshot")
        anchor_commit = oid(snapshot["commit"], f"{where}.authority_snapshot.commit")
        anchor_tree = oid(snapshot["tree"], f"{where}.authority_snapshot.tree")
        anchor_path = text(snapshot["path"], f"{where}.authority_snapshot.path")
        anchor_blob = oid(snapshot["blob"], f"{where}.authority_snapshot.blob")
        require(anchor_commit != root_commit, f"{where} enrollment authority must pre-exist the root")
        require(v4.git_is_ancestor(repository, anchor_commit, root_commit), f"{where} authority snapshot is not a strict ancestor")
        require(v4.git(repository, "rev-parse", f"{anchor_commit}^{{tree}}") == anchor_tree, f"{where} authority tree mismatch")
        require(v4.git(repository, "rev-parse", f"{anchor_commit}:{anchor_path}") == anchor_blob, f"{where} authority blob mismatch")
        historical = v4.parse_json_bytes(
            subprocess.run(
                ["git", "-C", str(repository), "show", f"{anchor_commit}:{anchor_path}"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout,
            f"{where} historical authority registry",
        )
        require(isinstance(historical, dict), f"{where} historical authority registry invalid")
        exact_keys(historical, {"schema", "status", "authorities", "requirements"}, f"{where} historical authority registry")
        require(historical["schema"] == ENROLLMENT_AUTHORITIES_SCHEMA and historical["status"] == "ACTIVE", f"{where} historical authority is not active")
        authority_key_id = text(receipt["authority_key_id"], f"{where}.authority_key_id")
        candidates = [item for item in historical["authorities"] if isinstance(item, dict) and item.get("key_id") == authority_key_id]
        require(len(candidates) == 1, f"{where} external authority key is not unique in the historical snapshot")
        authority = candidates[0]
        exact_keys(authority, {"key_id", "public_key", "role", "principal_id", "enabled"}, f"{where} authority")
        require(authority["enabled"] is True and authority["role"] == "platform-security-enrollment-authority", f"{where} authority purpose mismatch")
        raw_key = v4.decode_base64url(authority["public_key"], f"{where} authority public key")
        require(authority_key_id == "sha256:" + hashlib.sha256(raw_key).hexdigest(), f"{where} authority key ID mismatch")
        require(
            any(
                isinstance(item, dict)
                and item.get("key_id") == authority_key_id
                and item.get("public_key") == authority["public_key"]
                and item.get("enabled") is True
                for item in current_authorities["authorities"]
            ),
            f"{where} historical authority is not retained in the current source registry",
        )
        try:
            Ed25519PublicKey.from_public_bytes(raw_key).verify(
                v4.decode_base64(receipt["signature"], f"{where}.signature"),
                v3.canonical(statement),
            )
        except InvalidSignature as exc:
            raise v3.ContractError(f"{where} external enrollment signature invalid") from exc
        accepted.add(root_key_id)
    require(accepted == set(roots), "external enrollment receipt closure differs from evidence roots")
    return digest(receipts_document)


def verify_successor_source(payload: dict[str, Any], repository: Path, v4_context: dict[str, Any]) -> None:
    source = payload["source"]
    require(isinstance(source, dict), "v5 source must be an object")
    exact_keys(source, {"commit", "tree", "blobs"}, "v5 source")
    commit = oid(source["commit"], "v5 source.commit")
    tree = oid(source["tree"], "v5 source.tree")
    require(commit not in REJECTED_COMMITS, "v5 source is a preserved rejected candidate")
    require(commit == v4_context["payload"]["source"]["commit"], "v5 and v4 source commits differ")
    require(tree == v4_context["payload"]["source"]["tree"], "v5 and v4 source trees differ")
    blobs = source["blobs"]
    require(isinstance(blobs, list), "v5 source.blobs must be a list")
    bound = {item["path"]: item["oid"] for item in blobs if isinstance(item, dict) and set(item) == {"path", "oid"}}
    require(set(bound) == SUCCESSOR_SOURCE_PATHS, "v5 successor source blob closure mismatch")
    for path, expected in bound.items():
        require(v4.git(repository, "rev-parse", f"{commit}:{path}") == oid(expected, f"v5 source blob {path}"), f"v5 source blob mismatch: {path}")


def verify_successor_signatures(packet: dict[str, Any], roots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    exact_keys(packet, {"payload", "payload_sha256", "signatures"}, "v5 authority bundle")
    payload = packet["payload"]
    require(isinstance(payload, dict), "v5 payload must be an object")
    require(packet["payload_sha256"] == digest(payload), "v5 payload digest mismatch")
    signatures = packet["signatures"]
    require(isinstance(signatures, list) and len(signatures) == 2, "v5 requires exactly two signatures")
    roles: set[str] = set()
    principals: set[str] = set()
    for index, signature in enumerate(signatures):
        where = f"v5 signatures[{index}]"
        require(isinstance(signature, dict), f"{where} must be an object")
        exact_keys(signature, {"key_id", "role", "signature"}, where)
        key_id = text(signature["key_id"], f"{where}.key_id")
        require(key_id in roots, f"{where} key is not externally enrolled")
        root = roots[key_id]
        require(signature["role"] == root["role"], f"{where} role mismatch")
        require(root["role"] not in roles and root["principal_id"] not in principals, "v5 signatures must use distinct roles and principals")
        try:
            Ed25519PublicKey.from_public_bytes(root["raw_key"]).verify(
                v4.decode_base64(signature["signature"], f"{where}.signature"),
                v3.canonical(payload),
            )
        except InvalidSignature as exc:
            raise v3.ContractError(f"{where} signature invalid") from exc
        roles.add(root["role"])
        principals.add(root["principal_id"])
    require(roles == v4.REQUIRED_ROLES, "v5 collector and reviewer signatures are both required")
    return payload


def binding_authority_records(
    v4_entries: dict[str, dict[str, Any]],
    namespaces: list[str],
    mode: str,
) -> list[dict[str, Any]]:
    raw: dict[tuple[str, str], list[dict[str, Any]]] = {}
    roles: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for namespace, resource in sorted(v4.rbac_endpoints(namespaces)):
        items = v4_entries[f"k8s/rbac/{namespace or '_cluster'}/{resource}"]["body"]["items"]
        raw[(namespace, resource)] = items
        if resource in {"roles", "clusterroles"}:
            for item in items:
                scope = namespace if resource == "roles" else ""
                roles[(resource, scope, item["metadata"]["name"])] = item.get("rules", [])
    mutation_verbs = {"create", "update", "patch", "delete", "deletecollection", "*"}
    sensitive_resources = {
        "pods", "replicationcontrollers", "deployments", "statefulsets", "daemonsets",
        "replicasets", "jobs", "cronjobs", "networkpolicies", "roles", "rolebindings",
        "clusterroles", "clusterrolebindings", "validatingadmissionpolicies",
        "validatingadmissionpolicybindings", "secrets", "serviceaccounts",
        "serviceaccounts/token",
        *v4.CREDENTIAL_PIVOT_RESOURCES,
        "certificatesigningrequests", "certificatesigningrequests/approval", "signers",
    }

    def dangerous_rule(rule: dict[str, Any]) -> bool:
        verbs = set(rule.get("verbs", []))
        resources = set(rule.get("resources", []))
        groups = set(rule.get("apiGroups", []))
        if "*" in verbs:
            return True
        if verbs & {"impersonate", "bind", "escalate", "approve"}:
            return True
        if "create" in verbs and (
            "serviceaccounts/token" in resources
            or "certificatesigningrequests" in resources
            or "*" in resources
        ):
            return True
        if verbs & {"update", "patch"} and (
            "certificatesigningrequests/approval" in resources or "*" in resources
        ):
            return True
        if verbs & {"get", "list", "watch"} and (
            "secrets" in resources or "*" in resources
        ) and ("" in groups or "*" in groups):
            return True
        if verbs & {"create", "update", "patch"} and (
            "serviceaccounts" in resources or "*" in resources
        ) and ("" in groups or "*" in groups):
            return True
        if v4.credential_pivot_rule(rule):
            return True
        return bool(
            "impersonate" in verbs
            and (
                "*" in groups
                or "authentication.k8s.io" in groups
                or "" in groups
            )
            and resources & {"users", "groups", "serviceaccounts", "uids", "userextras", "*"}
        )
    records: list[dict[str, Any]] = []
    for (namespace, resource), items in sorted(raw.items()):
        if resource not in {"rolebindings", "clusterrolebindings"}:
            continue
        for binding in items:
            role_ref = binding.get("roleRef", {})
            role_resource = "roles" if role_ref.get("kind") == "Role" else "clusterroles"
            role_scope = namespace if role_resource == "roles" else ""
            rules = roles.get((role_resource, role_scope, role_ref.get("name", "")), [])
            if mode == "dangerous":
                selected = any(dangerous_rule(rule) for rule in rules)
            else:
                selected = any(
                    mutation_verbs & set(rule.get("verbs", []))
                    and ({"*"} | sensitive_resources) & set(rule.get("resources", []))
                    for rule in rules
                )
            if not selected:
                continue
            metadata = binding["metadata"]
            for subject in binding.get("subjects", []):
                records.append(
                    {
                        "binding_resource": resource,
                        "binding_namespace": namespace,
                        "binding_name": metadata["name"],
                        "binding_uid": metadata["uid"],
                        "role_ref_kind": role_ref.get("kind", ""),
                        "role_ref_name": role_ref.get("name", ""),
                        "rules_sha256": digest(rules),
                        "subject_kind": subject.get("kind", ""),
                        "subject_namespace": subject.get("namespace", ""),
                        "subject_name": subject.get("name", ""),
                    }
                )
    return sorted(
        records,
        key=lambda item: (
            item["binding_resource"], item["binding_namespace"], item["binding_name"],
            item["subject_kind"], item["subject_namespace"], item["subject_name"],
        ),
    )


def effective_labels(item: dict[str, Any], resource: str) -> dict[str, str]:
    if resource == "pods":
        value = item.get("metadata", {}).get("labels", {})
    elif resource == "cronjobs":
        value = item.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
    else:
        value = item.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
    require(isinstance(value, dict) and all(isinstance(key, str) and isinstance(entry, str) for key, entry in value.items()), "peer workload labels invalid")
    return value


def owner_references(item: dict[str, Any]) -> list[dict[str, Any]]:
    owners = item.get("metadata", {}).get("ownerReferences", [])
    require(isinstance(owners, list), "ownerReferences must be a list")
    return [
        {
            "api_version": owner.get("apiVersion", ""),
            "kind": owner.get("kind", ""),
            "name": owner.get("name", ""),
            "uid": str(owner.get("uid", "")),
            "controller": owner.get("controller") is True,
        }
        for owner in owners
    ]


def normalize_peer_workload(item: dict[str, Any], namespace: str, resource: str) -> dict[str, Any]:
    metadata = item.get("metadata", {})
    labels = effective_labels(item, resource)
    return {
        "namespace": namespace,
        "resource": resource,
        "api_version": v4.WORKLOAD_TYPES[resource][0],
        "kind": v4.WORKLOAD_TYPES[resource][1],
        "name": text(metadata.get("name"), "peer workload name"),
        "uid": text(metadata.get("uid"), "peer workload UID"),
        "resource_version": text(metadata.get("resourceVersion"), "peer workload resourceVersion"),
        "labels": labels,
        "labels_sha256": digest(labels),
        "owner_references": owner_references(item),
        "content_sha256": digest(v4.stable_object(item)),
    }


def verify_peer_inventory(
    entries: dict[str, dict[str, Any]],
    v4_context: dict[str, Any],
    authorization: dict[str, Any],
) -> dict[str, Any]:
    workloads: list[dict[str, Any]] = []
    for (namespace, resource), endpoint in sorted(PEER_ENDPOINTS.items()):
        name = f"k8s/peer-workloads/{namespace}/{resource}"
        body = v4.list_body(entries[name], endpoint, name)
        workloads.extend(normalize_peer_workload(item, namespace, resource) for item in body["items"])
    workloads = sorted(workloads, key=lambda item: (item["namespace"], item["resource"], item["name"], item["uid"]))
    require(digest(workloads) == authorization["peer_workload_inventory_sha256"], "peer workload inventory is not content-derived")

    cnpg_entry = entries["k8s/cnpg-cluster/fs2-data/fs2-control-db"]
    require(cnpg_entry["request"]["method"] == "GET" and cnpg_entry["request"]["path"] == CNPG_CLUSTER_ENDPOINT, "CNPG Cluster request identity mismatch")
    cnpg = cnpg_entry["body"]
    require(cnpg.get("apiVersion") == "postgresql.cnpg.io/v1" and cnpg.get("kind") == "Cluster", "CNPG owner is not the expected API type")
    cnpg_metadata = cnpg.get("metadata", {})
    require(cnpg_metadata.get("namespace") == "fs2-data" and cnpg_metadata.get("name") == "fs2-control-db", "CNPG owner identity mismatch")
    cnpg_identity = {
        "api_version": "postgresql.cnpg.io/v1",
        "kind": "Cluster",
        "namespace": "fs2-data",
        "name": "fs2-control-db",
        "uid": text(cnpg_metadata.get("uid"), "CNPG Cluster UID"),
        "resource_version": text(cnpg_metadata.get("resourceVersion"), "CNPG Cluster resourceVersion"),
        "content_sha256": digest(v4.stable_object(cnpg)),
    }
    require(digest(cnpg_identity) == authorization["cnpg_cluster_identity_sha256"], "CNPG Cluster identity is not content-derived")

    database_pods = v4_context["entries"]["k8s/database-pods/fs2-data"]["body"]["items"]
    for pod in database_pods:
        owners = owner_references(pod)
        require(
            any(
                owner["controller"]
                and owner["api_version"] == cnpg_identity["api_version"]
                and owner["kind"] == cnpg_identity["kind"]
                and owner["name"] == cnpg_identity["name"]
                and owner["uid"] == cnpg_identity["uid"]
                for owner in owners
            ),
            "database Pod does not have the exact live CNPG Cluster owner",
        )

    operator_objects = [
        item
        for item in workloads
        if item["namespace"] == "cnpg-system"
        and item["labels"].get("app.kubernetes.io/name") == "cloudnative-pg"
    ]
    require(operator_objects, "CNPG operator inventory is empty")
    require(
        all(item["resource"] in {"deployments", "replicasets", "pods"} for item in operator_objects),
        "CNPG operator inventory contains an unsupported rollout controller kind",
    )
    deployment_objects = [item for item in operator_objects if item["resource"] == "deployments"]
    require(deployment_objects, "CNPG operator inventory has no Deployment rollout root")
    signed_rollout_lineages = authorization["rollout_lineages"]
    require(isinstance(signed_rollout_lineages, list), "signed CNPG rollout lineages must be a list")
    signed_lineages_by_root: dict[tuple[str, str, str, str, str], dict[str, str]] = {}
    for index, signed_lineage in enumerate(signed_rollout_lineages):
        where = f"rollout_lineages[{index}]"
        require(isinstance(signed_lineage, dict), f"{where} must be an object")
        exact_keys(
            signed_lineage,
            {"namespace", "api_version", "kind", "name", "uid", "lineage", "controller_username"},
            where,
        )
        key = (
            signed_lineage["namespace"],
            signed_lineage["api_version"],
            signed_lineage["kind"],
            signed_lineage["name"],
            signed_lineage["uid"],
        )
        require(key not in signed_lineages_by_root, f"{where} duplicates a rollout root")
        signed_lineages_by_root[key] = signed_lineage
    rollout_lineages: list[dict[str, str]] = []
    deployments_by_owner = {}
    for deployment in deployment_objects:
        lineage_identity = {
            "namespace": deployment["namespace"],
            "api_version": deployment["api_version"],
            "kind": deployment["kind"],
            "name": deployment["name"],
            "uid": deployment["uid"],
        }
        lineage = digest(lineage_identity)
        root_key = (
            lineage_identity["namespace"],
            lineage_identity["api_version"],
            lineage_identity["kind"],
            lineage_identity["name"],
            lineage_identity["uid"],
        )
        require(root_key in signed_lineages_by_root, "CNPG Deployment rollout root is absent from signed authorization")
        signed_lineage = signed_lineages_by_root[root_key]
        controller_username = text(signed_lineage["controller_username"], "rollout lineage controller_username")
        require(signed_lineage["lineage"] == lineage, "CNPG rollout lineage digest is not source-derived")
        require(
            deployment["labels"].get(ROLLOUT_LINEAGE_LABEL) == lineage,
            "CNPG Deployment template lacks its source-derived rollout lineage",
        )
        rollout_lineages.append({**lineage_identity, "lineage": lineage, "controller_username": controller_username})
        deployments_by_owner[(deployment["api_version"], deployment["kind"], deployment["name"], deployment["uid"])] = lineage
    rollout_lineages = sorted(rollout_lineages, key=lambda item: (item["namespace"], item["name"], item["uid"]))
    require(
        len(signed_lineages_by_root) == len(rollout_lineages)
        and rollout_lineages == signed_rollout_lineages,
        "CNPG rollout lineage closure differs from the signed authorization",
    )

    replica_sets = [item for item in operator_objects if item["resource"] == "replicasets"]
    replica_sets_by_owner: dict[tuple[str, str, str, str], str] = {}
    for replica_set in replica_sets:
        roots = {
            deployments_by_owner[(owner["api_version"], owner["kind"], owner["name"], owner["uid"])]
            for owner in replica_set["owner_references"]
            if owner["controller"]
            and (owner["api_version"], owner["kind"], owner["name"], owner["uid"]) in deployments_by_owner
        }
        require(len(roots) == 1, "CNPG ReplicaSet does not resolve to one exact signed Deployment root")
        lineage = next(iter(roots))
        require(replica_set["labels"].get(ROLLOUT_LINEAGE_LABEL) == lineage, "CNPG ReplicaSet rollout lineage differs from its Deployment root")
        replica_sets_by_owner[(replica_set["api_version"], replica_set["kind"], replica_set["name"], replica_set["uid"])] = lineage

    for pod in (item for item in operator_objects if item["resource"] == "pods"):
        roots = {
            replica_sets_by_owner[(owner["api_version"], owner["kind"], owner["name"], owner["uid"])]
            for owner in pod["owner_references"]
            if owner["controller"]
            and (owner["api_version"], owner["kind"], owner["name"], owner["uid"]) in replica_sets_by_owner
        }
        require(len(roots) == 1, "CNPG operator Pod does not resolve through one exact ReplicaSet to a signed Deployment root")
        require(pod["labels"].get(ROLLOUT_LINEAGE_LABEL) == next(iter(roots)), "CNPG operator Pod rollout lineage differs from its Deployment root")

    parent_objects = [item for item in operator_objects if item["resource"] != "pods"]
    live_parents = {
        (item["namespace"], item["resource"], item["name"], item["uid"]): item
        for item in parent_objects
    }
    controller_usernames = authorization["cnpg_controller_usernames"]
    require(isinstance(controller_usernames, list) and controller_usernames == sorted(set(controller_usernames)) and controller_usernames, "CNPG controller usernames must be an exact non-empty set")
    admitted_controllers = {
        principal["username"]
        for principal in v4_context["legacy"]["rbac_inventory"]["principals"]
        if principal["class"] == "controller"
    }
    require(set(controller_usernames) <= admitted_controllers, "CNPG controller identity is outside the authenticated principal inventory")
    require(
        all(item["controller_username"] in controller_usernames for item in rollout_lineages),
        "CNPG rollout lineage actor is outside the exact authenticated controller set",
    )
    controller_identities: list[dict[str, Any]] = []
    principals_by_username = {
        principal["username"]: principal
        for principal in v4_context["legacy"]["rbac_inventory"]["principals"]
        if principal["class"] == "controller"
    }
    for username in controller_usernames:
        principal = principals_by_username[username]
        observed_identity = v4.subject_from_review(
            v4_context["entries"][f"k8s/identity/{principal['id']}/selfsubjectreview"]["body"],
            f"CNPG controller {principal['id']}",
        )
        require(observed_identity["username"] == username, "CNPG controller username is not authenticator-derived")
        controller_identities.append(observed_identity)
    parents = authorization["authorized_peer_parents"]
    require(isinstance(parents, list), "authorized_peer_parents must be a list")
    seen: set[tuple[str, str, str, str]] = set()
    normalized_parents: list[dict[str, Any]] = []
    for index, parent in enumerate(parents):
        where = f"authorized_peer_parents[{index}]"
        require(isinstance(parent, dict), f"{where} must be an object")
        exact_keys(parent, {"namespace", "resource", "api_version", "kind", "name", "uid", "controller_username"}, where)
        key = (parent["namespace"], parent["resource"], parent["name"], parent["uid"])
        require(key in live_parents and key not in seen, f"{where} does not resolve to one unique live operator parent")
        live = live_parents[key]
        require(parent["api_version"] == live["api_version"] and parent["kind"] == live["kind"], f"{where} API identity mismatch")
        require(parent["controller_username"] in controller_usernames, f"{where} controller identity is not authenticated")
        seen.add(key)
        normalized_parents.append(parent)
    require(seen == set(live_parents), "authorized peer parents are not the exact live CNPG operator-controller closure")

    parent_owner_keys = {
        (item["api_version"], item["kind"], item["name"], item["uid"])
        for item in parent_objects
    }
    for pod in (item for item in operator_objects if item["resource"] == "pods"):
        require(
            any(
                owner["controller"]
                and (owner["api_version"], owner["kind"], owner["name"], owner["uid"]) in parent_owner_keys
                for owner in pod["owner_references"]
            ),
            "CNPG operator Pod does not resolve to one exact live operator parent",
        )
    return {
        "peer_workload_inventory_sha256": authorization["peer_workload_inventory_sha256"],
        "cnpg_cluster_uid": cnpg_identity["uid"],
        "cnpg_cluster_identity_sha256": authorization["cnpg_cluster_identity_sha256"],
        "authorized_peer_parents_json": json.dumps(
            sorted(normalized_parents, key=lambda item: (item["namespace"], item["resource"], item["name"], item["uid"])),
            sort_keys=True,
            separators=(",", ":"),
        ),
        "rollout_lineages_json": json.dumps(
            rollout_lineages,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "cnpg_controller_usernames_json": json.dumps(controller_usernames, separators=(",", ":")),
        "cnpg_controller_identities_json": json.dumps(
            controller_identities,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def render_bootstrap_contract(contract: dict[str, Any], executor: dict[str, Any]) -> dict[str, Any]:
    protected_names = contract["protected_names"]
    require(isinstance(protected_names, list) and protected_names == sorted(set(protected_names)), "bootstrap protected names must be sorted and unique")
    replacements = {
        "${protected_names_json}": json.dumps(protected_names, separators=(",", ":")),
        "${executor_uid_json}": json.dumps(executor["uid"]),
        "${executor_username_json}": json.dumps(executor["username"]),
        "${executor_group_count}": str(len(executor["groups"])),
        "${executor_groups_json}": json.dumps(executor["groups"], separators=(",", ":")),
        "${executor_extra_json}": json.dumps(executor["extra"], sort_keys=True, separators=(",", ":")),
    }

    def visit(value: Any) -> Any:
        if isinstance(value, str):
            for needle, replacement in replacements.items():
                value = value.replace(needle, replacement)
            return value
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            return {key: visit(item) for key, item in value.items()}
        return value

    return visit(contract)


def normalized_admission_object(item: Any, where: str) -> dict[str, Any]:
    require(isinstance(item, dict), f"{where} must be an object")
    metadata = item.get("metadata", {})
    require(isinstance(metadata, dict), f"{where}.metadata must be an object")
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    require(
        isinstance(labels, dict)
        and all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items()),
        f"{where}.metadata.labels must be a string map",
    )
    require(
        isinstance(annotations, dict)
        and all(isinstance(key, str) and isinstance(value, str) for key, value in annotations.items()),
        f"{where}.metadata.annotations must be a string map",
    )
    require(
        item.get("apiVersion") == "admissionregistration.k8s.io/v1"
        and item.get("kind") in {"ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"},
        f"{where} API identity is invalid",
    )
    return {
        "api_version": item["apiVersion"],
        "kind": item["kind"],
        "name": text(metadata.get("name"), f"{where}.metadata.name"),
        "uid": metadata.get("uid", ""),
        "resource_version": metadata.get("resourceVersion", ""),
        "labels": dict(sorted(labels.items())),
        "annotations": dict(sorted(annotations.items())),
        "spec": item.get("spec", {}),
    }


def admission_source_surface(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in item.items()
        if key not in {"uid", "resource_version"}
    }


def admission_objects_by_name(items: list[Any], where: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        normalized = normalized_admission_object(item, f"{where}[{index}]")
        require(normalized["name"] not in result, f"{where} contains duplicate {normalized['name']}")
        result[normalized["name"]] = normalized
    return result


def verify_bootstrap_guard(
    entries: dict[str, dict[str, Any]],
    query: dict[str, str],
    executor: dict[str, Any],
    authorization: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    contract = source_json(query, "bootstrap_guard_contract_path", "expected_bootstrap_guard_contract_sha256", "bootstrap guard contract")
    exact_keys(contract, {"schema", "protected_names", "policy", "binding"}, "bootstrap guard contract")
    require(contract["schema"] == "fs2-serve.nebius.ai/sai20-bootstrap-guard/v5", "bootstrap guard contract schema mismatch")
    rendered = render_bootstrap_contract(contract, executor)
    policy_body = v4.list_body(entries["k8s/admission/validatingadmissionpolicies"], ADMISSION_POLICY_ENDPOINT, "ValidatingAdmissionPolicy list")
    binding_body = v4.list_body(entries["k8s/admission/validatingadmissionpolicybindings"], ADMISSION_BINDING_ENDPOINT, "ValidatingAdmissionPolicyBinding list")
    policies = [item for item in policy_body["items"] if item.get("metadata", {}).get("name") == rendered["policy"]["name"]]
    bindings = [item for item in binding_body["items"] if item.get("metadata", {}).get("name") == rendered["binding"]["name"]]
    require(len(policies) == 1 and len(bindings) == 1, "pre-existing bootstrap guard policy and binding must both be uniquely active")
    reserved_successor_names = set(rendered["protected_names"]) - {
        rendered["policy"]["name"],
        rendered["binding"]["name"],
    }
    require(TRANSITION_ADMISSION_NAMES < reserved_successor_names, "bootstrap contract omits a transition admission name")
    live_admission = admission_objects_by_name(
        policy_body["items"] + binding_body["items"],
        "pre-activation admission objects",
    )
    preexisting_successor_names = set(live_admission) & reserved_successor_names
    if not preexisting_successor_names:
        mode = "INITIAL"
    else:
        require(
            preexisting_successor_names == reserved_successor_names,
            "guarded successor admission set is partial and cannot be renewed",
        )
        mode = "RENEWAL"
    old_objects = [live_admission[name] for name in sorted(preexisting_successor_names)]
    require(
        all(
            isinstance(item["uid"], str)
            and bool(item["uid"])
            and isinstance(item["resource_version"], str)
            and bool(item["resource_version"])
            for item in old_objects
        ),
        "renewal predecessor admission objects require exact live UID and resourceVersion",
    )
    transition = authorization["successor_transition"]
    require(isinstance(transition, dict), "successor_transition must be an object")
    exact_keys(
        transition,
        {"mode", "old_objects_sha256", "planned_objects_sha256"},
        "successor_transition",
    )
    require(transition["mode"] == mode, "signed successor transition mode differs from live state")
    require(
        transition["old_objects_sha256"] == digest(old_objects),
        "signed successor transition does not bind the exact old admission objects",
    )
    sha256(transition["planned_objects_sha256"], "successor_transition.planned_objects_sha256")
    policy = policies[0]
    binding = bindings[0]
    require(policy.get("spec") == rendered["policy"]["spec"], "pre-existing bootstrap guard policy differs from source contract")
    require(binding.get("spec") == rendered["binding"]["spec"], "pre-existing bootstrap guard binding differs from source contract")
    identity = {
        "policy_name": rendered["policy"]["name"],
        "policy_uid": text(policy.get("metadata", {}).get("uid"), "bootstrap policy UID"),
        "policy_resource_version": text(policy.get("metadata", {}).get("resourceVersion"), "bootstrap policy resourceVersion"),
        "policy_spec_sha256": digest(policy["spec"]),
        "binding_name": rendered["binding"]["name"],
        "binding_uid": text(binding.get("metadata", {}).get("uid"), "bootstrap binding UID"),
        "binding_resource_version": text(binding.get("metadata", {}).get("resourceVersion"), "bootstrap binding resourceVersion"),
        "binding_spec_sha256": digest(binding["spec"]),
        "transition_mode": mode,
        "old_objects_sha256": transition["old_objects_sha256"],
        "planned_objects_sha256": transition["planned_objects_sha256"],
    }
    require(digest(identity) == authorization["bootstrap_guard_sha256"], "bootstrap guard receipt is not content-derived")
    return authorization["bootstrap_guard_sha256"], {
        "mode": mode,
        "reserved_names": reserved_successor_names,
        "old_objects": {item["name"]: item for item in old_objects},
        "old_objects_sha256": transition["old_objects_sha256"],
        "planned_objects_sha256": transition["planned_objects_sha256"],
    }


def verify_supplemental_bundle(
    query: dict[str, str],
    v4_result: dict[str, str],
    v4_context: dict[str, Any],
    roots: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], dict[str, Any]]:
    repository = Path(query["repository_root"])
    enrollment_receipts_sha256 = verify_external_root_enrollment(query, roots, repository)
    packet_bytes = v3.safe_read(query["successor_bundle_path"], "successor_bundle_path", 32 * 1024 * 1024)
    packet = v4.parse_json_bytes(packet_bytes, "v5 authority bundle")
    require(isinstance(packet, dict), "v5 authority bundle must be an object")
    payload = verify_successor_signatures(packet, roots)
    exact_keys(
        payload,
        {
            "schema", "status", "observed_at", "valid_until", "source", "cluster",
            "v4_bundle_sha256", "external_enrollment_receipts_sha256", "authorization",
            "independent_review", "transcript",
        },
        "v5 payload",
    )
    require(payload["schema"] == SCHEMA and payload["status"] == "ACCEPTED", "v5 bundle is not accepted")
    observed = v3.parse_time(payload["observed_at"], "v5 payload.observed_at")
    valid_until = v3.parse_time(payload["valid_until"], "v5 payload.valid_until")
    now = datetime.now(timezone.utc)
    require(observed <= now <= valid_until and valid_until - observed <= timedelta(minutes=30), "v5 bundle is expired or overlong")
    require(now - observed <= timedelta(minutes=10), "v5 evidence is older than ten minutes")
    require(payload["v4_bundle_sha256"] == v4_result["bundle_sha256"], "v5 does not bind the exact v4 bundle")
    require(payload["external_enrollment_receipts_sha256"] == enrollment_receipts_sha256, "v5 does not bind the external enrollment receipts")
    verify_successor_source(payload, repository, v4_context)
    require(payload["cluster"] == v4_context["payload"]["cluster"], "v5 and v4 cluster identities differ")

    transcript = payload["transcript"]
    require(isinstance(transcript, list), "v5 transcript must be a list")
    entries: dict[str, dict[str, Any]] = {}
    observed_times: list[datetime] = []
    for index, raw_entry in enumerate(transcript):
        entry = v4.verify_transcript_entry(raw_entry, payload["cluster"], f"v5 transcript[{index}]")
        require(entry["name"] not in entries, f"duplicate v5 transcript entry {entry['name']}")
        entries[entry["name"]] = entry
        observed_times.append(entry["observed"])
    require(set(entries) == set(SUPPLEMENTAL_ENDPOINTS), "v5 transcript does not have the exact source-defined supplemental request closure")
    require(observed_times and min(observed_times) >= observed and max(observed_times) <= valid_until, "v5 transcript observations fall outside its evidence window")
    collector_digest = v4_context["payload"]["collector"]["credential_subject_sha256"]
    for name, entry in entries.items():
        require(entry["origin"] == "kubernetes-api", f"{name} must be a Kubernetes API observation")
        require(entry["request"]["method"] == "GET" and entry["request"]["path"] == SUPPLEMENTAL_ENDPOINTS[name], f"{name} request identity mismatch")
        require(entry["request"]["credential_subject_sha256"] == collector_digest, f"{name} was not collected by the authenticated collector")

    authorization = payload["authorization"]
    require(isinstance(authorization, dict), "v5 authorization must be an object")
    exact_keys(
        authorization,
        {
            "all_dangerous_rbac_bindings", "all_sensitive_mutation_bindings",
            "cluster_authority_review_sha256", "peer_workload_inventory_sha256",
            "cnpg_cluster_identity_sha256", "authorized_peer_parents",
            "cnpg_controller_usernames", "rollout_lineages", "principal_uids",
            "bootstrap_guard_sha256", "successor_transition",
            "provider_group_response_sha256", "provider_observer_sha256",
            "provider_observer_credential_subject_sha256",
        },
        "v5 authorization",
    )
    for field in (
        "cluster_authority_review_sha256", "peer_workload_inventory_sha256",
        "cnpg_cluster_identity_sha256", "bootstrap_guard_sha256",
        "provider_group_response_sha256", "provider_observer_sha256",
        "provider_observer_credential_subject_sha256",
    ):
        sha256(authorization[field], f"v5 authorization.{field}")
    dangerous = binding_authority_records(v4_context["entries"], v4_context["namespaces"], "dangerous")
    sensitive = binding_authority_records(v4_context["entries"], v4_context["namespaces"], "sensitive")
    require(dangerous == authorization["all_dangerous_rbac_bindings"], "cluster-inclusive dangerous RBAC closure differs")
    require(sensitive == authorization["all_sensitive_mutation_bindings"], "cluster-inclusive sensitive mutation closure differs")
    cluster_review = {
        "dangerous": [item for item in dangerous if item["binding_resource"] == "clusterrolebindings"],
        "sensitive": [item for item in sensitive if item["binding_resource"] == "clusterrolebindings"],
    }
    require(digest(cluster_review) == authorization["cluster_authority_review_sha256"], "cluster authority review is not content-derived")
    admitted_subjects = {
        (principal["subject"]["kind"], principal["subject"]["namespace"], principal["subject"]["name"])
        for principal in v4_context["legacy"]["rbac_inventory"]["principals"]
    }
    custodian_subjects = {
        (principal["subject"]["kind"], principal["subject"]["namespace"], principal["subject"]["name"])
        for principal in v4_context["legacy"]["rbac_inventory"]["principals"]
        if principal["class"] == "custodian"
    }
    require(
        all(
            (record["subject_kind"], record["subject_namespace"], record["subject_name"])
            in custodian_subjects
            for record in dangerous
        ),
        "dangerous RoleBinding/ClusterRoleBinding authority is not constrained to an exact custodian subject",
    )
    require(
        all(
            (record["subject_kind"], record["subject_namespace"], record["subject_name"])
            in admitted_subjects
            for record in sensitive
        ),
        "sensitive RoleBinding/ClusterRoleBinding authority is not constrained to an exact admitted principal",
    )
    principal_uids = {
        principal["id"]: v4.subject_from_review(
            v4_context["entries"][f"k8s/identity/{principal['id']}/selfsubjectreview"]["body"],
            f"v5 principal {principal['id']}",
        )["uid"]
        for principal in v4_context["legacy"]["rbac_inventory"]["principals"]
    }
    require(
        isinstance(authorization["principal_uids"], dict)
        and all(isinstance(key, str) and isinstance(value, str) and value for key, value in authorization["principal_uids"].items())
        and principal_uids == authorization["principal_uids"],
        "principal UID registry is not authenticator-derived",
    )
    principal_identities = []
    for principal in v4_context["legacy"]["rbac_inventory"]["principals"]:
        observed = v4.subject_from_review(
            v4_context["entries"][f"k8s/identity/{principal['id']}/selfsubjectreview"]["body"],
            f"v5 exact principal {principal['id']}",
        )
        principal_identities.append({"id": principal["id"], "class": principal["class"], **observed})
    principal_identities.sort(key=lambda item: item["id"])
    provider_body = v4_context["entries"]["nebius/legacy-group-membership"]["body"]
    require(digest(provider_body) == authorization["provider_group_response_sha256"], "v5 provider group receipt differs from the authenticated response")

    executor = v4_context["executor"]
    peer_result = verify_peer_inventory(entries, v4_context, authorization)
    bootstrap_sha, successor_transition = verify_bootstrap_guard(entries, query, executor, authorization)
    review = payload["independent_review"]
    require(isinstance(review, dict), "v5 independent_review must be an object")
    exact_keys(review, {"verdict", "commit", "tree", "reviewer_principal_id", "reviewed_at", "findings_sha256"}, "v5 independent_review")
    reviewer_roots = [root for root in roots.values() if root["role"] == "independent-reviewer"]
    require(
        len(reviewer_roots) == 1
        and review["verdict"] == "ACCEPTED"
        and review["commit"] == payload["source"]["commit"]
        and review["tree"] == payload["source"]["tree"]
        and review["reviewer_principal_id"] == reviewer_roots[0]["principal_id"],
        "v5 independent review does not accept the exact successor source",
    )
    reviewed_at = v3.parse_time(review["reviewed_at"], "v5 independent_review.reviewed_at")
    require(observed <= reviewed_at <= valid_until, "v5 independent review falls outside the evidence window")
    sha256(review["findings_sha256"], "v5 independent_review.findings_sha256")

    result = {
        **v4_result,
        **peer_result,
        "verified": "true",
        "successor_verified": "true",
        "successor_bundle_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "successor_payload_sha256": packet["payload_sha256"],
        "external_enrollment_receipts_sha256": enrollment_receipts_sha256,
        "cluster_authority_review_sha256": authorization["cluster_authority_review_sha256"],
        "bootstrap_guard_sha256": bootstrap_sha,
        "successor_transition_mode": successor_transition["mode"],
        "successor_old_objects_sha256": successor_transition["old_objects_sha256"],
        "successor_planned_objects_sha256": successor_transition["planned_objects_sha256"],
        "provider_observer_sha256": authorization["provider_observer_sha256"],
        "principal_identities_json": json.dumps(
            principal_identities,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    context = {
        "payload": payload,
        "entries": entries,
        "authorization": authorization,
        "successor_transition": successor_transition,
        "v4": v4_context,
    }
    return result, context


def expected_transition_admission_objects(
    query: dict[str, str],
    context: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    value = v4.parse_json_bytes(
        query["expected_successor_admission_objects_json"].encode("utf-8"),
        "source-rendered successor admission objects",
    )
    require(isinstance(value, list), "source-rendered successor admission objects must be a list")
    normalized = admission_objects_by_name(value, "source-rendered successor admission objects")
    require(set(normalized) == TRANSITION_ADMISSION_NAMES, "source-rendered successor admission object set differs")
    surfaces = [admission_source_surface(normalized[name]) for name in sorted(normalized)]
    require(
        digest(surfaces) == context["successor_transition"]["planned_objects_sha256"],
        "source-rendered successor admission digest differs from the signed transition",
    )
    return normalized


def reobserve_supplemental_kubernetes(
    query: dict[str, str],
    context: dict[str, Any],
    *,
    allow_successor_admission_additions: bool = False,
) -> str:
    expected_objects = (
        expected_transition_admission_objects(query, context)
        if allow_successor_admission_additions
        else {}
    )
    observed_transition_objects: dict[str, dict[str, Any]] = {}
    for name, entry in context["entries"].items():
        current = v4.parse_json_bytes(v4.live_get(query, context["v4"], entry["request"]["path"]), f"apply {name}")
        if name == "k8s/cnpg-cluster/fs2-data/fs2-control-db":
            require(
                digest(v4.stable_object(current)) == digest(v4.stable_object(entry["body"])),
                "v5 apply-time CNPG Cluster singleton differs from the signed observation",
            )
            continue
        metadata = current.get("metadata", {}) if isinstance(current, dict) else {}
        require(isinstance(current, dict) and isinstance(current.get("items"), list), f"v5 apply response is not a list: {name}")
        require(metadata.get("continue", "") == "" and metadata.get("remainingItemCount", 0) in {0, None}, f"v5 apply response is paginated: {name}")
        if allow_successor_admission_additions and name in {
            "k8s/admission/validatingadmissionpolicies",
            "k8s/admission/validatingadmissionpolicybindings",
        }:
            before = admission_objects_by_name(entry["body"]["items"], f"signed {name}")
            after = admission_objects_by_name(current["items"], f"apply {name}")
            transition_names = set(after) & TRANSITION_ADMISSION_NAMES
            expected_names = {
                object_name
                for object_name, expected in expected_objects.items()
                if expected["kind"] == (
                    "ValidatingAdmissionPolicy"
                    if name.endswith("validatingadmissionpolicies")
                    else "ValidatingAdmissionPolicyBinding"
                )
            }
            require(transition_names == expected_names, f"v5 apply transition object set differs: {name}")
            mode = context["successor_transition"]["mode"]
            if mode == "INITIAL":
                require(
                    set(after) - set(before) == expected_names
                    and not (set(before) & expected_names),
                    f"initial activation did not add exactly the source-rendered successor set: {name}",
                )
            else:
                require(set(after) == set(before), f"renewal changed admission object names: {name}")
                for object_name in expected_names:
                    require(
                        before[object_name]["uid"]
                        and before[object_name]["uid"] == after[object_name]["uid"],
                        f"renewal replaced protected admission object: {object_name}",
                    )
            for object_name, old_object in before.items():
                if object_name not in expected_names:
                    require(
                        object_name in after and after[object_name] == old_object,
                        f"v5 apply changed a non-transition admission object: {object_name}",
                    )
            for object_name in expected_names:
                observed = after[object_name]
                require(
                    admission_source_surface(observed)
                    == admission_source_surface(expected_objects[object_name]),
                    f"v5 apply successor object is not source-exact: {object_name}",
                )
                observed_transition_objects[object_name] = observed
            continue
        require(digest(v4.stable_object(current)) == digest(v4.stable_object(entry["body"])), f"v5 apply-time re-observation differs: {name}")
    if not allow_successor_admission_additions:
        return ""
    require(set(observed_transition_objects) == TRANSITION_ADMISSION_NAMES, "source-exact successor activation is incomplete")
    observed_surfaces = [
        admission_source_surface(observed_transition_objects[name])
        for name in sorted(observed_transition_objects)
    ]
    observed_digest = digest(observed_surfaces)
    require(
        observed_digest == context["successor_transition"]["planned_objects_sha256"],
        "live successor admission digest differs from signed source render",
    )
    return observed_digest


def reobserve_provider_group(query: dict[str, str], context: dict[str, Any]) -> None:
    observer = Path(query["provider_group_observer_path"])
    require(observer.is_absolute(), "provider_group_observer_path must be absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    observer_fd = os.open(str(observer), flags)
    sealed_fd = -1
    try:
        before = os.fstat(observer_fd)
        require(stat.S_ISREG(before.st_mode), "provider group observer must be a regular file")
        require(before.st_size > 0 and before.st_size <= 256 * 1024 * 1024, "provider group observer size is invalid")
        require(before.st_mode & 0o111 != 0, "provider group observer is not executable")
        require(before.st_uid == 0, "provider group observer must be owned by root")
        require(before.st_mode & 0o022 == 0, "provider group observer must not be group- or world-writable")
        sealed_fd = os.memfd_create("sai20-provider-observer", os.MFD_ALLOW_SEALING)
        observer_hash = hashlib.sha256()
        remaining = before.st_size
        while remaining > 0:
            chunk = os.read(observer_fd, min(1024 * 1024, remaining))
            require(chunk != b"", "provider group observer changed during authenticated read")
            observer_hash.update(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(sealed_fd, chunk[offset:])
                require(written > 0, "provider group observer sealed copy write failed")
                offset += written
            remaining -= len(chunk)
        require(os.read(observer_fd, 1) == b"", "provider group observer exceeds its authenticated size")
        authorization = context["authorization"]
        require(observer_hash.hexdigest() == authorization["provider_observer_sha256"], "apply provider observer differs from the signed executable")
        v4.require_static_elf(sealed_fd, before.st_size, "provider group observer")
        os.fchmod(sealed_fd, before.st_mode & 0o555)
        seal_mask = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        fcntl.fcntl(sealed_fd, fcntl.F_ADD_SEALS, seal_mask)
        require(fcntl.fcntl(sealed_fd, fcntl.F_GET_SEALS) == seal_mask, "provider group observer snapshot is not immutable")
        os.lseek(sealed_fd, 0, os.SEEK_SET)
        legacy_entry = context["v4"]["entries"]["nebius/legacy-group-membership"]
        request = {
            "schema": "fs2-serve.nebius.ai/sai20-provider-group-observer-request/v1",
            "nonce": text(query["apply_nonce"], "apply_nonce"),
            "method": "POST",
            "path": "/nebius.iam.v1.GroupMembershipService/List",
            "body": legacy_entry["request_body"],
            "endpoint_sha256": context["payload"]["cluster"]["provider_iam_endpoint_sha256"],
            "ca_sha256": context["payload"]["cluster"]["provider_iam_ca_sha256"],
        }
        started = datetime.now(timezone.utc)
        completed = subprocess.run(
            [f"/proc/self/fd/{sealed_fd}"],
            input=v3.canonical(request),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(sealed_fd,),
            env={"HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"},
        )
        after = os.fstat(observer_fd)
        require(
            (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "provider group observer changed while it was executing",
        )
    finally:
        if sealed_fd >= 0:
            os.close(sealed_fd)
        os.close(observer_fd)
    require(completed.returncode == 0, "apply-time provider group observer failed")
    observed_entry = v4.verify_transcript_entry(
        v4.parse_json_bytes(completed.stdout, "apply provider group transcript"),
        context["payload"]["cluster"],
        "apply provider group transcript",
    )
    require(observed_entry["origin"] == "nebius-iam", "apply provider group response lacks provider origin")
    require(observed_entry["request"]["method"] == request["method"] and observed_entry["request"]["path"] == request["path"], "apply provider group request identity mismatch")
    require(observed_entry["request_body"] == request["body"], "apply provider group request body mismatch")
    require(observed_entry["request"]["credential_subject_sha256"] == authorization["provider_observer_credential_subject_sha256"], "apply provider observer credential is not the signed identity")
    require(started <= observed_entry["observed"] <= datetime.now(timezone.utc) + timedelta(seconds=5), "apply provider observation is not fresh")
    require(digest(observed_entry["body"]) == authorization["provider_group_response_sha256"], "provider group membership changed between signed observation and apply")
    require(
        observed_entry["body"].get("members") == []
        and observed_entry["body"].get("next_page_token", "") == "",
        "legacy provider group is no longer authoritatively empty",
    )


def v4_query(query: dict[str, str]) -> dict[str, str]:
    keys = {
        "mode", "authority_bundle_path", "legacy_v3_handoff_path", "root_registry_path",
        "ingress_contract_path", "expected_root_registry_sha256",
        "expected_ingress_contract_file_sha256", "repository_root", "expected_project_id",
        "expected_cluster_id", "run_id",
    }
    if query["mode"] in {"identity", "apply"}:
        keys |= {"kubeconfig_path", "kube_context", "kubectl_path", "apply_nonce"}
    return {key: query[key] for key in keys}


def main() -> int:
    try:
        query = v4.parse_json_bytes(sys.stdin.buffer.read(), "Terraform v5 external query")
        require(isinstance(query, dict), "Terraform v5 external query must be an object")
        common = {
            "mode", "authority_bundle_path", "legacy_v3_handoff_path", "root_registry_path",
            "ingress_contract_path", "expected_root_registry_sha256",
            "expected_ingress_contract_file_sha256", "repository_root", "expected_project_id",
            "expected_cluster_id", "run_id", "successor_bundle_path",
            "enrollment_authorities_path", "expected_enrollment_authorities_sha256",
            "root_enrollment_receipts_path", "expected_root_enrollment_receipts_sha256",
            "bootstrap_guard_contract_path", "expected_bootstrap_guard_contract_sha256",
        }
        runtime = {"kubeconfig_path", "kube_context", "kubectl_path", "provider_group_observer_path", "apply_nonce"}
        apply_only = {"expected_successor_admission_objects_json"}
        require(query.get("mode") in {"plan", "identity", "apply"}, "v5 mode must be plan, identity or apply")
        required = set(common)
        if query["mode"] in {"identity", "apply"}:
            required |= runtime
        if query["mode"] == "apply":
            required |= apply_only
        require(set(query) == required, "Terraform v5 external query keys invalid")
        base_result, base_context, roots = v4.verify_bundle(v4_query(query))
        result, context = verify_supplemental_bundle(query, base_result, base_context, roots)
        if query["mode"] == "identity":
            v4.verify_apply_identity(v4_query(query), base_context)
            reobserve_supplemental_kubernetes(query, context)
            result["apply_nonce"] = text(query["apply_nonce"], "apply_nonce")
            result["sealed_kubeconfig_sha256"] = base_context["_kubeconfig_sha256"]
            result["identity_reobserved"] = "true"
            result["apply_reobserved"] = "false"
            result["bootstrap_reobserved"] = "true"
            result["provider_group_reobserved"] = "false"
            result["successor_source_exact_reobserved"] = "false"
        elif query["mode"] == "apply":
            v4.verify_apply(v4_query(query), base_context, roots)
            successor_activation_sha256 = reobserve_supplemental_kubernetes(
                query,
                context,
                allow_successor_admission_additions=True,
            )
            reobserve_provider_group(query, context)
            result["apply_nonce"] = text(query["apply_nonce"], "apply_nonce")
            result["sealed_kubeconfig_sha256"] = base_context["_kubeconfig_sha256"]
            result["identity_reobserved"] = "true"
            result["apply_reobserved"] = "true"
            result["bootstrap_reobserved"] = "true"
            result["provider_group_reobserved"] = "true"
            result["successor_source_exact_reobserved"] = "true"
            result["successor_activation_sha256"] = successor_activation_sha256
        else:
            result["identity_reobserved"] = "false"
            result["apply_reobserved"] = "false"
            result["bootstrap_reobserved"] = "false"
            result["provider_group_reobserved"] = "false"
            result["successor_source_exact_reobserved"] = "false"
        v4.close_kubectl(base_context)
        json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    except (v3.ContractError, OSError, UnicodeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        json.dump({"error": str(exc)}, sys.stderr, sort_keys=True)
        sys.stderr.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
