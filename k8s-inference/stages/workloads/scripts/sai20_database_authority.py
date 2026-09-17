#!/usr/bin/env python3
"""Verify the signed SAI-20 database-network activation authority.

The verifier is intentionally plan-time and fail-closed.  It does not query a
cluster.  A separate, independently authorized collector must enumerate the
live objects and RBAC state, sign the canonical packet, and deliver it with the
security-owner public key fingerprint through the release process.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


SCHEMA = "fs2-serve.nebius.ai/sai20-database-authority/v3"
REJECTED_COMMITS = {
    "cea63190aca6548d8be961a9432cc7cc1277721e",
    "07faac62c6854a7b7947f97f59b5b7b1030813fd",
    "850c1aeb134196b36250b5e8bd20cf7c1aa1c0aa",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OID_RE = re.compile(r"^[0-9a-f]{40}$")
RESOURCE_LISTS = {
    (namespace, resource)
    for namespace in ("fs2-system", "fs2-observability")
    for resource in (
        "pods",
        "replicationcontrollers",
        "deployments",
        "statefulsets",
        "daemonsets",
        "replicasets",
        "jobs",
        "cronjobs",
    )
}
RBAC_LISTS = {
    ("fs2-system", "roles"),
    ("fs2-system", "rolebindings"),
    ("fs2-observability", "roles"),
    ("fs2-observability", "rolebindings"),
    ("", "clusterroles"),
    ("", "clusterrolebindings"),
}
DATABASE_PROFILES = {
    "control-plane",
    "grafana",
    "acceptance",
    "storage-legacy",
    "storage-v2",
    "storage-v3",
}
STORAGE_GENERATIONS = {
    "storage-reconciler": "storage-legacy",
    "storage-reconciler-v2": "storage-v2",
    "storage-reconciler-v3": "storage-v3",
}
CONTROL_PLANE_COMPONENTS = {
    "bootstrap-access",
    "bootstrap-scientific-access",
    "bootstrap-website-access",
    "gateway",
    "maintenance",
    "migration",
    "model-controller",
    "storage-disclosure",
    *STORAGE_GENERATIONS,
}
MUTABLE_RESOURCES = {
    "replicationcontrollers": "",
    "deployments": "apps",
    "statefulsets": "apps",
    "daemonsets": "apps",
    "replicasets": "apps",
    "jobs": "batch",
    "cronjobs": "batch",
}
REQUIRED_CURRENT_BLOBS = {
    "k8s-inference/stages/workloads/sai20_database_authority_v3.tf",
    "k8s-inference/stages/workloads/sai20_database_custody.tf",
    "k8s-inference/stages/workloads/sai20_network_isolation.tf",
    "k8s-inference/stages/workloads/outputs.tf",
    "k8s-inference/stages/workloads/scripts/sai20_database_authority.py",
    "k8s-inference/stages/workloads/versions.tf",
    "k8s-inference/tests/test_sai20_database_authority_v3.py",
    "k8s-inference/docs/SAI-20-NETWORK-ISOLATION.md",
}
SAI08_COMMIT = "6eb13e345c8b17420d1217a70d83e4974497b2b0"
SAI08_TREE = "bd55519891c3f653f11465bf59e997170ff8bf4a"
SAI08_BLOBS = {
    "k8s-inference/security/customer-storage-egress-boundary/main.tf":
        "530c3a6ad53a871238aa1819ffa06f585aeaf86f",
    "k8s-inference/charts/security/customer-storage-reconciler-v2/templates/reconciler.yaml":
        "01966d2e3cc3caa72e70c7b0b96737fd0e5beac7",
}


class ContractError(ValueError):
    """The authority packet is not sufficient for activation."""


def fail(message: str) -> None:
    raise ContractError(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def exact_keys(value: dict[str, Any], keys: set[str], where: str) -> None:
    require(set(value) == keys, f"{where} keys must be exactly {sorted(keys)}")


def string(value: Any, where: str) -> str:
    require(isinstance(value, str) and value != "", f"{where} must be a non-empty string")
    return value


def sha256(value: Any, where: str) -> str:
    result = string(value, where)
    require(SHA256_RE.fullmatch(result) is not None, f"{where} must be lowercase SHA-256")
    return result


def oid(value: Any, where: str) -> str:
    result = string(value, where)
    require(OID_RE.fullmatch(result) is not None, f"{where} must be a lowercase Git object ID")
    return result


def unique_strings(value: Any, where: str, *, nonempty: bool = True) -> list[str]:
    require(isinstance(value, list), f"{where} must be a list")
    result = [string(item, f"{where}[]") for item in value]
    require(len(result) == len(set(result)), f"{where} must not contain duplicates")
    if nonempty:
        require(result, f"{where} must not be empty")
    return result


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def parse_time(value: Any, where: str) -> datetime:
    raw = string(value, where)
    require(raw.endswith("Z"), f"{where} must be UTC with a Z suffix")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{where} is not RFC3339") from exc
    require(parsed.tzinfo == timezone.utc, f"{where} must be UTC")
    return parsed


def safe_read(path_value: Any, where: str, max_bytes: int) -> bytes:
    supplied_path = Path(string(path_value, where))
    require(supplied_path.is_absolute(), f"{where} must be absolute")
    path = Path(os.path.abspath(supplied_path))
    parts = path.parts[1:]
    require(parts and all(part not in {"", ".", ".."} for part in parts), f"{where} path is invalid")
    parent_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            stat_before = os.fstat(fd)
            require(stat.S_ISREG(stat_before.st_mode), f"{where} must be a regular file")
            require(0 < stat_before.st_size <= max_bytes, f"{where} must be non-empty and at most {max_bytes} bytes")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            stat_after = os.fstat(fd)
            require(len(data) <= max_bytes, f"{where} exceeds {max_bytes} bytes")
            require(len(data) == stat_before.st_size, f"{where} was not read completely")
            require(
                (stat_before.st_dev, stat_before.st_ino, stat_before.st_size, stat_before.st_mtime_ns)
                == (stat_after.st_dev, stat_after.st_ino, stat_after.st_size, stat_after.st_mtime_ns),
                f"{where} changed while it was read",
            )
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    require(completed.returncode == 0, f"Git object verification failed: {' '.join(args)}")
    return completed.stdout.strip()


def parse_json(data: bytes, where: str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            require(key not in value, f"{where} contains duplicate field {key!r}")
            value[key] = item
        return value

    try:
        return json.loads(data, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{where} is not valid JSON") from exc


def verify_list_receipt(value: Any, where: str) -> int:
    require(isinstance(value, dict), f"{where} must be an object")
    exact_keys(
        value,
        {"resource_version", "continue_token", "remaining_item_count", "item_count", "items_sha256"},
        where,
    )
    string(value["resource_version"], f"{where}.resource_version")
    require(value["continue_token"] == "", f"{where}.continue_token must prove list completion")
    require(value["remaining_item_count"] == 0, f"{where}.remaining_item_count must be zero")
    require(isinstance(value["item_count"], int) and value["item_count"] >= 0, f"{where}.item_count invalid")
    sha256(value["items_sha256"], f"{where}.items_sha256")
    return value["item_count"]


def selector_matches(selector: dict[str, Any], labels: dict[str, str], where: str) -> bool:
    exact_keys(selector, {"matchLabels", "matchExpressions"}, where)
    match_labels = selector["matchLabels"]
    expressions = selector["matchExpressions"]
    require(isinstance(match_labels, dict), f"{where}.matchLabels must be an object")
    require(
        all(isinstance(key, str) and isinstance(value, str) for key, value in match_labels.items()),
        f"{where}.matchLabels must contain strings",
    )
    require(isinstance(expressions, list), f"{where}.matchExpressions must be a list")
    if any(labels.get(key) != value for key, value in match_labels.items()):
        return False
    for index, expression in enumerate(expressions):
        item_where = f"{where}.matchExpressions[{index}]"
        require(isinstance(expression, dict), f"{item_where} must be an object")
        exact_keys(expression, {"key", "operator", "values"}, item_where)
        key = string(expression["key"], f"{item_where}.key")
        operator = expression["operator"]
        require(operator in {"In", "NotIn", "Exists", "DoesNotExist"}, f"{item_where}.operator invalid")
        values = unique_strings(expression["values"], f"{item_where}.values", nonempty=False)
        if operator in {"In", "NotIn"}:
            require(values, f"{item_where}.values must not be empty for {operator}")
        else:
            require(not values, f"{item_where}.values must be empty for {operator}")
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and (key not in labels or labels[key] in values):
            return False
        if operator == "Exists" and key not in labels:
            return False
        if operator == "DoesNotExist" and key in labels:
            return False
    return True


def selector_explicitly_excludes_database(selector: dict[str, Any]) -> bool:
    match_labels = selector["matchLabels"]
    if "cnpg.io/cluster" in match_labels and match_labels["cnpg.io/cluster"] != "fs2-control-db":
        return True
    for expression in selector["matchExpressions"]:
        if expression["key"] != "cnpg.io/cluster":
            continue
        operator = expression["operator"]
        values = expression["values"]
        if operator == "In" and "fs2-control-db" not in values:
            return True
        if operator == "NotIn" and "fs2-control-db" in values:
            return True
        if operator == "DoesNotExist":
            return True
    return False


def database_profile(labels: dict[str, str]) -> str | None:
    if labels.get("app.kubernetes.io/name") == "grafana":
        return "grafana"
    if (
        labels.get("app.kubernetes.io/component") == "acceptance"
        and labels.get("app.kubernetes.io/managed-by") == "terraform"
        and labels.get("app.kubernetes.io/part-of") == "fs2-serve"
        and labels.get("fs2.nebius.ai/environment") == "disposable"
        and bool(labels.get("fs2.nebius.ai/run-id"))
    ):
        return "acceptance"
    if (
        labels.get("app.kubernetes.io/name") == "fs2-serve-control-plane"
        and labels.get("app.kubernetes.io/instance") == "fs2-serve-control-plane"
        and labels.get("app.kubernetes.io/component") in CONTROL_PLANE_COMPONENTS
    ):
        component = labels["app.kubernetes.io/component"]
        return STORAGE_GENERATIONS.get(component, "control-plane")
    return None


def verify_source(payload: dict[str, Any], query: dict[str, str]) -> tuple[str, str]:
    source = payload["source"]
    require(isinstance(source, dict), "source must be an object")
    exact_keys(source, {"commit", "tree", "blobs", "upstream_sources"}, "source")
    commit = oid(source["commit"], "source.commit")
    tree = oid(source["tree"], "source.tree")
    require(commit not in REJECTED_COMMITS, "source.commit is a preserved rejected candidate")

    repository = Path(query["repository_root"])
    require(repository.is_absolute(), "repository_root must be absolute")
    require(git(repository, "rev-parse", "HEAD") == commit, "signed source.commit does not equal checkout HEAD")
    require(git(repository, "rev-parse", "HEAD^{tree}") == tree, "signed source.tree does not equal checkout tree")
    require(git(repository, "diff", "--name-only") == "", "tracked worktree differs from signed commit")
    require(git(repository, "diff", "--cached", "--name-only") == "", "index differs from signed commit")

    blobs = source["blobs"]
    require(isinstance(blobs, list), "source.blobs must be a list")
    by_path: dict[str, str] = {}
    for index, blob in enumerate(blobs):
        where = f"source.blobs[{index}]"
        require(isinstance(blob, dict), f"{where} must be an object")
        exact_keys(blob, {"path", "oid"}, where)
        path = string(blob["path"], f"{where}.path")
        require(path not in by_path, f"duplicate source blob {path}")
        by_path[path] = oid(blob["oid"], f"{where}.oid")
    require(set(by_path) == REQUIRED_CURRENT_BLOBS, "source.blobs must bind the complete SAI-20 source set")
    for path, expected_oid in by_path.items():
        require(git(repository, "rev-parse", f"{commit}:{path}") == expected_oid, f"source blob mismatch: {path}")

    upstream = source["upstream_sources"]
    require(isinstance(upstream, list) and len(upstream) == 1, "source.upstream_sources must bind exact SAI-08")
    sai08 = upstream[0]
    require(isinstance(sai08, dict), "SAI-08 source binding must be an object")
    exact_keys(sai08, {"commit", "tree", "blobs"}, "source.upstream_sources[0]")
    require(sai08["commit"] == SAI08_COMMIT and sai08["tree"] == SAI08_TREE, "SAI-08 commit/tree mismatch")
    require(git(repository, "rev-parse", f"{SAI08_COMMIT}^{{tree}}") == SAI08_TREE, "SAI-08 tree unavailable")
    require(sai08["blobs"] == SAI08_BLOBS, "SAI-08 blob binding mismatch")
    for path, expected_oid in SAI08_BLOBS.items():
        require(git(repository, "rev-parse", f"{SAI08_COMMIT}:{path}") == expected_oid, f"SAI-08 blob mismatch: {path}")
    return commit, tree


def verify_network_inventory(payload: dict[str, Any], query: dict[str, str]) -> list[str]:
    inventory = payload["network_policy_inventory"]
    require(isinstance(inventory, dict), "network_policy_inventory must be an object")
    exact_keys(
        inventory,
        {"namespace", "phase", "database_pod_list", "database_pods", "list", "items", "planned_policy"},
        "network_policy_inventory",
    )
    require(inventory["namespace"] == "fs2-data", "network policy inventory namespace mismatch")
    require(inventory["phase"] in {"pre-activation", "steady-state"}, "network policy inventory phase invalid")
    database_pod_count = verify_list_receipt(inventory["database_pod_list"], "network_policy_inventory.database_pod_list")
    database_pods = inventory["database_pods"]
    require(
        isinstance(database_pods, list) and len(database_pods) == database_pod_count and database_pods,
        "database Pod inventory must be complete and non-empty",
    )
    require(
        digest(database_pods) == inventory["database_pod_list"]["items_sha256"],
        "database Pod list digest mismatch",
    )
    database_labels: dict[str, dict[str, str]] = {}
    for index, pod in enumerate(database_pods):
        where = f"network_policy_inventory.database_pods[{index}]"
        require(isinstance(pod, dict), f"{where} must be an object")
        exact_keys(pod, {"name", "uid", "resource_version", "labels", "labels_sha256"}, where)
        string(pod["name"], f"{where}.name")
        uid = string(pod["uid"], f"{where}.uid")
        require(uid not in database_labels, f"duplicate database Pod UID {uid}")
        string(pod["resource_version"], f"{where}.resource_version")
        labels = pod["labels"]
        require(isinstance(labels, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()), f"{where}.labels invalid")
        require(labels.get("cnpg.io/cluster") == "fs2-control-db", f"{where} is not a control database Pod")
        require(digest(labels) == pod["labels_sha256"], f"{where}.labels digest mismatch")
        database_labels[uid] = labels
    count = verify_list_receipt(inventory["list"], "network_policy_inventory.list")
    items = inventory["items"]
    require(isinstance(items, list) and len(items) == count, "network policy item count mismatch")
    require(digest(items) == inventory["list"]["items_sha256"], "network policy list digest mismatch")
    names: list[str] = []
    overlaps: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        where = f"network_policy_inventory.items[{index}]"
        require(isinstance(item, dict), f"{where} must be an object")
        exact_keys(
            item,
            {
                "name", "uid", "resource_version", "spec_sha256", "pod_selector", "selector_sha256",
                "selected_database_pod_uids", "selects_control_database", "disposition",
            },
            where,
        )
        name = string(item["name"], f"{where}.name")
        names.append(name)
        string(item["uid"], f"{where}.uid")
        string(item["resource_version"], f"{where}.resource_version")
        sha256(item["spec_sha256"], f"{where}.spec_sha256")
        selector = item["pod_selector"]
        require(isinstance(selector, dict), f"{where}.pod_selector must be an object")
        require(digest(selector) == item["selector_sha256"], f"{where}.selector_sha256 mismatch")
        selected_uids = unique_strings(item["selected_database_pod_uids"], f"{where}.selected_database_pod_uids", nonempty=False)
        require(set(selected_uids) <= set(database_labels), f"{where} selects an unknown database Pod")
        recomputed_uids = sorted(
            uid for uid, labels in database_labels.items() if selector_matches(selector, labels, f"{where}.pod_selector")
        )
        require(sorted(selected_uids) == recomputed_uids, f"{where} database Pod selection was misclassified")
        require(type(item["selects_control_database"]) is bool, f"{where}.selects_control_database must be boolean")
        require(item["selects_control_database"] == bool(recomputed_uids), f"{where}.selects_control_database mismatch")
        if item["selects_control_database"]:
            overlaps.append(item)
            require(item["disposition"] == "canonical-exact", f"{where} overlapping policy is not canonical")
        else:
            require(item["disposition"] == "non-overlapping", f"{where} disposition invalid")
            require(
                selector_explicitly_excludes_database(selector),
                f"{where} does not permanently exclude the control database cluster label",
            )
    require(len(names) == len(set(names)), "network policy inventory contains duplicate names")

    planned = inventory["planned_policy"]
    require(isinstance(planned, dict), "planned_policy must be an object")
    exact_keys(planned, {"name", "spec_sha256", "source_sha256"}, "planned_policy")
    require(planned["name"] == "fs2-control-db-ingress", "planned policy name mismatch")
    planned_spec = sha256(planned["spec_sha256"], "planned_policy.spec_sha256")
    require(planned["source_sha256"] == query["expected_policy_source_sha256"], "planned policy source hash mismatch")
    if inventory["phase"] == "pre-activation":
        require(not overlaps, "pre-activation inventory contains an overlapping database ingress policy")
    else:
        require(len(overlaps) == 1, "steady-state inventory must contain exactly one database ingress policy")
        require(overlaps[0]["name"] == planned["name"], "alternate database ingress policy is present")
        require(overlaps[0]["spec_sha256"] == planned_spec, "live canonical policy spec differs from signed plan")
    return sorted(set(names + [planned["name"]]))


def verify_workload_inventory(payload: dict[str, Any]) -> None:
    inventory = payload["workload_inventory"]
    require(isinstance(inventory, dict), "workload_inventory must be an object")
    exact_keys(inventory, {"lists", "objects", "storage_generations"}, "workload_inventory")
    lists = inventory["lists"]
    require(isinstance(lists, list), "workload_inventory.lists must be a list")
    seen_lists: set[tuple[str, str]] = set()
    list_receipts: dict[tuple[str, str], dict[str, Any]] = {}
    total = 0
    for index, receipt in enumerate(lists):
        where = f"workload_inventory.lists[{index}]"
        require(isinstance(receipt, dict), f"{where} must be an object")
        exact_keys(receipt, {"namespace", "resource", "list"}, where)
        key = (string(receipt["namespace"], f"{where}.namespace"), string(receipt["resource"], f"{where}.resource"))
        require(key not in seen_lists, f"duplicate workload list receipt {key}")
        seen_lists.add(key)
        list_receipts[key] = receipt["list"]
        total += verify_list_receipt(receipt["list"], f"{where}.list")
    require(seen_lists == RESOURCE_LISTS, "workload inventory does not cover every Pod/controller list")

    objects = inventory["objects"]
    require(isinstance(objects, list) and len(objects) == total, "workload object count mismatch")
    seen_objects: set[tuple[str, str, str]] = set()
    storage_profiles_seen: set[str] = set()
    for index, item in enumerate(objects):
        where = f"workload_inventory.objects[{index}]"
        require(isinstance(item, dict), f"{where} must be an object")
        exact_keys(
            item,
            {
                "namespace", "resource", "name", "uid", "resource_version", "effective_labels",
                "effective_labels_sha256", "database_peer", "client_profile", "disposition",
            },
            where,
        )
        key = (
            string(item["namespace"], f"{where}.namespace"),
            string(item["resource"], f"{where}.resource"),
            string(item["name"], f"{where}.name"),
        )
        require((key[0], key[1]) in RESOURCE_LISTS, f"{where} is outside inventory closure")
        require(key not in seen_objects, f"duplicate workload object {key}")
        seen_objects.add(key)
        string(item["uid"], f"{where}.uid")
        string(item["resource_version"], f"{where}.resource_version")
        labels = item["effective_labels"]
        require(isinstance(labels, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()), f"{where}.effective_labels invalid")
        require(digest(labels) == item["effective_labels_sha256"], f"{where}.effective_labels digest mismatch")
        require(type(item["database_peer"]) is bool, f"{where}.database_peer must be boolean")
        if item["database_peer"]:
            profile = string(item["client_profile"], f"{where}.client_profile")
            require(profile in DATABASE_PROFILES, f"{where}.client_profile is not recognized")
            require(database_profile(labels) == profile, f"{where}.client_profile does not match effective labels")
            require(item["disposition"] == "approved-current", f"{where} database peer is not approved")
            component = labels.get("app.kubernetes.io/component", "")
            if component in STORAGE_GENERATIONS:
                require(profile == STORAGE_GENERATIONS[component], f"{where} storage profile mismatch")
                if component in {"storage-reconciler-v2", "storage-reconciler-v3"}:
                    require("fs2.nebius.ai/storage-egress-generation" in labels, f"{where} lacks storage egress generation")
                if component == "storage-reconciler-v3":
                    require("fs2.nebius.ai/storage-rollout-generation" in labels, f"{where} lacks v3 rollout generation")
                storage_profiles_seen.add(profile)
        else:
            require(database_profile(labels) is None, f"{where} conceals a database peer")
            require(item["client_profile"] is None and item["disposition"] == "not-a-database-peer", f"{where} non-peer disposition invalid")

    for key, receipt in list_receipts.items():
        selected = sorted(
            (item for item in objects if (item["namespace"], item["resource"]) == key),
            key=lambda item: (item["name"], item["uid"]),
        )
        require(len(selected) == receipt["item_count"], f"workload list {key} count is not authoritative")
        require(digest(selected) == receipt["items_sha256"], f"workload list {key} digest mismatch")

    generations = inventory["storage_generations"]
    require(isinstance(generations, list) and len(generations) == 3, "all three storage generations require disposition")
    by_component: dict[str, dict[str, Any]] = {}
    for index, generation in enumerate(generations):
        where = f"workload_inventory.storage_generations[{index}]"
        require(isinstance(generation, dict), f"{where} must be an object")
        exact_keys(generation, {"component", "status", "object_count", "receipt_sha256"}, where)
        component = string(generation["component"], f"{where}.component")
        require(component in STORAGE_GENERATIONS and component not in by_component, f"{where}.component invalid")
        require(generation["status"] in {"present", "retired"}, f"{where}.status invalid")
        require(isinstance(generation["object_count"], int) and generation["object_count"] >= 0, f"{where}.object_count invalid")
        sha256(generation["receipt_sha256"], f"{where}.receipt_sha256")
        profile = STORAGE_GENERATIONS[component]
        if generation["status"] == "present":
            require(generation["object_count"] > 0 and profile in storage_profiles_seen, f"{where} present generation not inventoried")
        else:
            require(generation["object_count"] == 0 and profile not in storage_profiles_seen, f"{where} retired generation still exists")
        by_component[component] = generation
    require(set(by_component) == set(STORAGE_GENERATIONS), "storage generation closure incomplete")


def verify_rbac(payload: dict[str, Any], legacy_group: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    inventory = payload["rbac_inventory"]
    require(isinstance(inventory, dict), "rbac_inventory must be an object")
    exact_keys(
        inventory,
        {"lists", "objects", "principals", "legacy_group", "legacy_group_members", "impersonation", "effective_permissions_sha256"},
        "rbac_inventory",
    )
    lists = inventory["lists"]
    require(isinstance(lists, list), "rbac_inventory.lists must be a list")
    seen_lists: set[tuple[str, str]] = set()
    list_receipts: dict[tuple[str, str], dict[str, Any]] = {}
    total = 0
    for index, receipt in enumerate(lists):
        where = f"rbac_inventory.lists[{index}]"
        require(isinstance(receipt, dict), f"{where} must be an object")
        exact_keys(receipt, {"namespace", "resource", "list"}, where)
        key = (receipt["namespace"], receipt["resource"])
        require(key in RBAC_LISTS and key not in seen_lists, f"{where} list identity invalid")
        seen_lists.add(key)
        list_receipts[key] = receipt["list"]
        total += verify_list_receipt(receipt["list"], f"{where}.list")
    require(seen_lists == RBAC_LISTS, "RBAC inventory does not cover all authoritative lists")
    objects = inventory["objects"]
    require(isinstance(objects, list) and len(objects) == total, "RBAC object count mismatch")
    seen_objects: set[tuple[str, str, str]] = set()
    for index, item in enumerate(objects):
        where = f"rbac_inventory.objects[{index}]"
        require(isinstance(item, dict), f"{where} must be an object")
        exact_keys(item, {"namespace", "resource", "name", "uid", "resource_version", "content_sha256"}, where)
        require((item["namespace"], item["resource"]) in RBAC_LISTS, f"{where} outside RBAC closure")
        key = (item["namespace"], item["resource"], string(item["name"], f"{where}.name"))
        require(key not in seen_objects, f"duplicate RBAC object {key}")
        seen_objects.add(key)
        string(item["uid"], f"{where}.uid")
        string(item["resource_version"], f"{where}.resource_version")
        sha256(item["content_sha256"], f"{where}.content_sha256")
    for key, receipt in list_receipts.items():
        selected = sorted(
            (item for item in objects if (item["namespace"], item["resource"]) == key),
            key=lambda item: (item["name"], item["uid"]),
        )
        require(len(selected) == receipt["item_count"], f"RBAC list {key} count is not authoritative")
        require(digest(selected) == receipt["items_sha256"], f"RBAC list {key} digest mismatch")

    require(inventory["legacy_group"] == legacy_group, "legacy broad-writer group mismatch")
    require(inventory["legacy_group_members"] == [], "legacy broad-writer group must be authoritatively empty")
    sha256(inventory["effective_permissions_sha256"], "rbac_inventory.effective_permissions_sha256")

    principals = inventory["principals"]
    require(isinstance(principals, list) and principals, "rbac_inventory.principals must not be empty")
    identifiers: set[str] = set()
    releases: list[dict[str, Any]] = []
    controllers: list[dict[str, Any]] = []
    custodian: dict[str, Any] | None = None
    for index, principal in enumerate(principals):
        where = f"rbac_inventory.principals[{index}]"
        require(isinstance(principal, dict), f"{where} must be an object")
        exact_keys(principal, {"id", "class", "username", "groups", "subject", "grants"}, where)
        identifier = string(principal["id"], f"{where}.id")
        require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", identifier) is not None, f"{where}.id invalid")
        require(identifier not in identifiers, f"duplicate principal id {identifier}")
        identifiers.add(identifier)
        username = string(principal["username"], f"{where}.username")
        groups = unique_strings(principal["groups"], f"{where}.groups")
        require("system:authenticated" in groups, f"{where} must be authenticated")
        require(legacy_group not in groups, f"{where} still inherits the broad legacy group")
        subject = principal["subject"]
        require(isinstance(subject, dict), f"{where}.subject must be an object")
        exact_keys(subject, {"kind", "name", "namespace"}, f"{where}.subject")
        require(subject["kind"] in {"User", "ServiceAccount"}, f"{where} group subjects are forbidden")
        string(subject["name"], f"{where}.subject.name")
        if subject["kind"] == "User":
            require(subject["namespace"] == "" and subject["name"] == username, f"{where} user subject mismatch")
        else:
            require(subject["namespace"] in {"fs2-system", "fs2-observability"}, f"{where} service-account namespace invalid")
            require(username == f"system:serviceaccount:{subject['namespace']}:{subject['name']}", f"{where} service-account username mismatch")
        grants = principal["grants"]
        require(isinstance(grants, list), f"{where}.grants must be a list")
        normalized_grants: list[dict[str, Any]] = []
        for grant_index, grant in enumerate(grants):
            grant_where = f"{where}.grants[{grant_index}]"
            require(isinstance(grant, dict), f"{grant_where} must be an object")
            exact_keys(grant, {"namespace", "resource", "operations", "names"}, grant_where)
            namespace = string(grant["namespace"], f"{grant_where}.namespace")
            resource = string(grant["resource"], f"{grant_where}.resource")
            require(namespace in {"fs2-system", "fs2-observability"}, f"{grant_where}.namespace invalid")
            require(resource in MUTABLE_RESOURCES, f"{grant_where}.resource is too broad")
            operations = unique_strings(grant["operations"], f"{grant_where}.operations")
            require(set(operations) <= {"CREATE", "UPDATE", "DELETE"}, f"{grant_where} unsupported operation forbidden")
            names = unique_strings(grant["names"], f"{grant_where}.names")
            require(all(re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", name) is not None for name in names), f"{grant_where}.names invalid")
            normalized_grants.append({"namespace": namespace, "resource": resource, "operations": operations, "names": names})
        principal_copy = {**principal, "groups": sorted(groups), "grants": normalized_grants}
        principal_class = principal["class"]
        if principal_class == "release":
            require(normalized_grants, f"{where} release principal needs exact grants")
            releases.append(principal_copy)
        elif principal_class == "controller":
            require(not normalized_grants and username.startswith("system:"), f"{where} controller grants invalid")
            controllers.append(principal_copy)
        elif principal_class == "custodian":
            require(not normalized_grants and custodian is None, f"{where} custodian invalid")
            custodian = principal_copy
        else:
            fail(f"{where}.class invalid")
    require(releases and controllers and custodian is not None, "release, controller and custodian principals are mandatory")

    impersonation = inventory["impersonation"]
    require(isinstance(impersonation, dict), "rbac_inventory.impersonation must be an object")
    exact_keys(impersonation, {"capable", "unaccounted"}, "rbac_inventory.impersonation")
    require(impersonation["unaccounted"] == [], "unaccounted impersonation authority exists")
    capable = impersonation["capable"]
    require(isinstance(capable, list), "impersonation capable list invalid")
    for index, entry in enumerate(capable):
        where = f"rbac_inventory.impersonation.capable[{index}]"
        require(isinstance(entry, dict), f"{where} must be an object")
        exact_keys(entry, {"principal_id", "guarded", "receipt_sha256"}, where)
        require(entry["principal_id"] in identifiers, f"{where} references unknown principal")
        require(entry["guarded"] is True, f"{where} is not guarded")
        sha256(entry["receipt_sha256"], f"{where}.receipt_sha256")
    return releases, controllers, custodian


def verify_packet(packet: dict[str, Any], query: dict[str, str], public_key_bytes: bytes) -> dict[str, str]:
    exact_keys(packet, {"payload", "payload_sha256", "signature"}, "packet")
    payload = packet["payload"]
    require(isinstance(payload, dict), "payload must be an object")
    exact_keys(
        payload,
        {
            "schema", "status", "authority", "source", "cluster", "network_policy_inventory",
            "workload_inventory", "rbac_inventory", "legacy_contract_sha256",
        },
        "payload",
    )
    require(payload["schema"] == SCHEMA and payload["status"] == "ACCEPTED", "packet is not an accepted v3 authority")
    payload_digest = digest(payload)
    require(packet["payload_sha256"] == payload_digest, "payload SHA-256 mismatch")
    try:
        signature = base64.b64decode(string(packet["signature"], "signature"), validate=True)
    except ValueError as exc:
        raise ContractError("signature is not canonical base64") from exc
    try:
        key = serialization.load_pem_public_key(public_key_bytes)
    except (TypeError, ValueError) as exc:
        raise ContractError("authority public key is not valid PEM") from exc
    require(isinstance(key, Ed25519PublicKey), "authority key must be Ed25519")
    try:
        key.verify(signature, canonical(payload))
    except InvalidSignature as exc:
        raise ContractError("authority signature verification failed") from exc

    authority = payload["authority"]
    require(isinstance(authority, dict), "authority must be an object")
    exact_keys(authority, {"key_id", "signer_principal_id", "observed_at", "valid_until", "review_receipt_sha256"}, "authority")
    require(authority["key_id"] == query["authority_key_id"], "authority key id mismatch")
    string(authority["signer_principal_id"], "authority.signer_principal_id")
    review_receipt = sha256(authority["review_receipt_sha256"], "authority.review_receipt_sha256")
    observed = parse_time(authority["observed_at"], "authority.observed_at")
    valid_until = parse_time(authority["valid_until"], "authority.valid_until")
    now = datetime.now(timezone.utc)
    require(observed <= now <= valid_until, "authority packet is not currently valid")
    require(valid_until - observed <= timedelta(hours=1), "authority packet validity exceeds one hour")
    require(now - observed <= timedelta(minutes=30), "authority inventory is older than thirty minutes")

    commit, tree = verify_source(payload, query)
    cluster = payload["cluster"]
    require(isinstance(cluster, dict), "cluster must be an object")
    exact_keys(cluster, {"project_id", "cluster_id", "context_sha256", "api_server_sha256"}, "cluster")
    require(cluster["project_id"] == query["expected_project_id"], "project identity mismatch")
    require(cluster["cluster_id"] == query["expected_cluster_id"], "cluster identity mismatch")
    context_sha = sha256(cluster["context_sha256"], "cluster.context_sha256")
    api_server_sha = sha256(cluster["api_server_sha256"], "cluster.api_server_sha256")
    require(payload["legacy_contract_sha256"] == query["legacy_contract_sha256"], "legacy custody input is not signed")

    policy_names = verify_network_inventory(payload, query)
    verify_workload_inventory(payload)
    releases, controllers, custodian = verify_rbac(payload, query["legacy_release_writer_group"])
    require(custodian["id"] == authority["signer_principal_id"], "signer is not the exact admitted custodian")
    return {
        "verified": "true",
        "handoff_sha256": hashlib.sha256(canonical(packet)).hexdigest(),
        "payload_sha256": payload_digest,
        "source_commit": commit,
        "source_tree": tree,
        "review_receipt_sha256": review_receipt,
        "cluster_context_sha256": context_sha,
        "api_server_sha256": api_server_sha,
        "network_policy_names_json": json.dumps(policy_names, separators=(",", ":")),
        "release_principals_json": json.dumps(releases, sort_keys=True, separators=(",", ":")),
        "controller_principals_json": json.dumps(controllers, sort_keys=True, separators=(",", ":")),
        "custodian_json": json.dumps(custodian, sort_keys=True, separators=(",", ":")),
    }


def main() -> int:
    try:
        query = parse_json(sys.stdin.buffer.read(), "Terraform external query")
        require(isinstance(query, dict), "Terraform external query must be an object")
        exact_keys(
            query,
            {
                "handoff_path", "public_key_path", "public_key_sha256", "authority_key_id",
                "repository_root", "expected_project_id", "expected_cluster_id",
                "expected_policy_source_sha256", "legacy_contract_sha256", "legacy_release_writer_group",
            },
            "query",
        )
        for key, value in query.items():
            string(value, f"query.{key}")
        public_key = safe_read(query["public_key_path"], "public_key_path", 16_384)
        require(hashlib.sha256(public_key).hexdigest() == query["public_key_sha256"], "authority public-key fingerprint mismatch")
        handoff = safe_read(query["handoff_path"], "handoff_path", 8 * 1024 * 1024)
        packet = parse_json(handoff, "authority packet")
        require(isinstance(packet, dict), "authority packet root must be an object")
        result = verify_packet(packet, query, public_key)
        json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    except (ContractError, OSError, json.JSONDecodeError, UnicodeError) as exc:
        json.dump({"error": str(exc)}, sys.stderr, sort_keys=True)
        sys.stderr.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
