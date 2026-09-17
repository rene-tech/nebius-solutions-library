#!/usr/bin/env python3
"""Prove a no-delete SAI-07 legacy-object closure or fail closed.

The hard no-delete contract forbids this tool from mutating Kubernetes. It
re-reads the exact signed inventory and cleanup fence, then emits a closure only
when the approved legacy-object set is already empty. A non-empty set is a real
integration blocker; this program does not rewrite or remove retained objects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sai07_inventory_projection import ProjectionError, live_projection  # noqa: E402

SCHEMA = "fs2-serve.nebius.ai/sai07-no-delete-closure/v4"
RESULT_SCHEMA = "fs2-serve.nebius.ai/sai07-no-delete-closure-result/v4"
NAMESPACE = "fs2-models"
MAX_OBJECTS = 128
KINDS = {
    "NetworkPolicy": ("networking.k8s.io", "v1", "networkpolicies"),
    "ServiceAccount": ("", "v1", "serviceaccounts"),
    "DaemonSet": ("apps", "v1", "daemonsets"),
}
FENCE_IDENTITIES = {
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-pod-security-legacy-cleanup-fence"),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
        "",
        "fs2-pod-security-legacy-cleanup-fence",
    ),
    ("v1", "ConfigMap", "fs2-system", "fs2-pod-security-rollout-ledger"),
}


class CleanupError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def path_for(kind: str, name: str) -> str:
    group, version, resource = KINDS[kind]
    prefix = f"/apis/{group}/{version}" if group else f"/api/{version}"
    return f"{prefix}/namespaces/{NAMESPACE}/{resource}/{quote(name, safe='')}"


def fence_path(api_version: str, kind: str, namespace: str, name: str) -> str:
    resource = {
        ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy"): "validatingadmissionpolicies",
        ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding"): "validatingadmissionpolicybindings",
        ("v1", "ConfigMap"): "configmaps",
    }[(api_version, kind)]
    if "/" in api_version:
        group, version = api_version.split("/", 1)
        return f"/apis/{group}/{version}/{resource}/{quote(name, safe='')}"
    return f"/api/{api_version}/namespaces/{namespace}/{resource}/{quote(name, safe='')}"


class Kubectl:
    def __init__(self, kubeconfig: Path, context: str) -> None:
        self.base = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context]

    def raw(self, uri: str, *, allow_absent: bool = False) -> dict[str, object] | None:
        completed = subprocess.run([*self.base, "get", "--raw", uri], check=False, capture_output=True, text=True)
        if (
            completed.returncode != 0
            and allow_absent
            and ("not found" in completed.stderr.lower() or "404" in completed.stderr)
        ):
            return None
        if completed.returncode != 0:
            raise CleanupError(f"read failed for {uri}: {completed.stderr.strip()}")
        return json.loads(completed.stdout)

def validate_manifest(raw: object) -> tuple[dict[str, object], list[dict[str, str]]]:
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "cluster_id",
        "run_id",
        "kube_system_uid",
        "baseline_artifact_sha256",
        "prior_inventory_sha256",
        "fence_objects",
        "objects",
    }:
        raise CleanupError("cleanup manifest fields differ from schema")
    if raw["schema"] != SCHEMA:
        raise CleanupError("cleanup manifest schema is unsupported")
    for field in ("cluster_id", "run_id", "kube_system_uid"):
        if not isinstance(raw[field], str) or not raw[field]:
            raise CleanupError(f"{field} is required")
    for field in ("baseline_artifact_sha256", "prior_inventory_sha256"):
        if (
            not isinstance(raw[field], str)
            or len(raw[field]) != 64
            or any(character not in "0123456789abcdef" for character in raw[field])
        ):
            raise CleanupError(f"{field} must be a lowercase SHA-256")
    fence_objects = raw["fence_objects"]
    if not isinstance(fence_objects, list) or len(fence_objects) != 3:
        raise CleanupError("fence_objects must contain the exact policy, binding, and ledger")
    fence_seen: set[tuple[str, str, str, str]] = set()
    for item in fence_objects:
        if not isinstance(item, dict) or set(item) != {
            "api_version",
            "kind",
            "namespace",
            "name",
            "uid",
            "resource_version",
            "object_sha256",
        }:
            raise CleanupError("cleanup fence object fields differ from schema")
        identity = (item["api_version"], item["kind"], item["namespace"], item["name"])
        if identity not in FENCE_IDENTITIES or identity in fence_seen:
            raise CleanupError("cleanup fence identity is duplicated or outside the exact contract")
        if not all(isinstance(item[field], str) and item[field] for field in ("uid", "resource_version")):
            raise CleanupError("cleanup fence exact identity is required")
        if not isinstance(item["object_sha256"], str) or len(item["object_sha256"]) != 64:
            raise CleanupError("cleanup fence object hash is malformed")
        fence_seen.add(identity)
    if fence_seen != FENCE_IDENTITIES:
        raise CleanupError("cleanup fence object inventory differs")

    objects = raw["objects"]
    if not isinstance(objects, list) or len(objects) > MAX_OBJECTS:
        raise CleanupError(f"objects must be a list of at most {MAX_OBJECTS}")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in objects:
        if not isinstance(item, dict) or set(item) != {
            "api_version",
            "kind",
            "namespace",
            "name",
            "uid",
            "resource_version",
            "object_sha256",
        }:
            raise CleanupError("cleanup object fields differ from schema")
        if item["kind"] not in KINDS or item["namespace"] != NAMESPACE:
            raise CleanupError("cleanup object kind or namespace is outside the bounded scope")
        expected_api_version = (
            "v1"
            if item["kind"] == "ServiceAccount"
            else ("apps/v1" if item["kind"] == "DaemonSet" else "networking.k8s.io/v1")
        )
        if item["api_version"] != expected_api_version:
            raise CleanupError("cleanup object apiVersion differs from its bounded kind")
        if not all(
            isinstance(item[key], str) and item[key] for key in ("name", "uid", "resource_version", "object_sha256")
        ):
            raise CleanupError("cleanup object exact identity fields are required")
        if len(item["object_sha256"]) != 64 or any(
            character not in "0123456789abcdef" for character in item["object_sha256"]
        ):
            raise CleanupError("cleanup object hash is malformed")
        identity = (item["kind"], item["name"])
        if identity in seen:
            raise CleanupError("cleanup object identities must be unique")
        seen.add(identity)
        normalized.append(item)
    return raw, normalized


def fence_projection(live: dict[str, object]) -> dict[str, object]:
    metadata = live.get("metadata")
    if not isinstance(metadata, dict):
        raise CleanupError("cleanup fence metadata is missing")
    return {
        "apiVersion": live.get("apiVersion"),
        "kind": live.get("kind"),
        "metadata": {
            "name": metadata.get("name"),
            "namespace": metadata.get("namespace", ""),
            "uid": metadata.get("uid"),
            "resourceVersion": metadata.get("resourceVersion"),
            "generation": metadata.get("generation"),
            "deletionTimestamp": metadata.get("deletionTimestamp"),
        },
        "spec": live.get("spec", {}),
        "data": live.get("data", {}),
    }


def validate_cleanup_fence(client: Kubectl, manifest: dict[str, object]) -> None:
    for expected in manifest["fence_objects"]:
        path = fence_path(
            expected["api_version"],
            expected["kind"],
            expected["namespace"],
            expected["name"],
        )
        live = client.raw(path)
        if live is None:  # pragma: no cover - mandatory read
            raise CleanupError("cleanup fence object is absent")
        metadata = live.get("metadata")
        if not isinstance(metadata, dict) or (
            live.get("apiVersion") != expected["api_version"]
            or live.get("kind") != expected["kind"]
            or metadata.get("name") != expected["name"]
            or metadata.get("namespace", "") != expected["namespace"]
            or metadata.get("uid") != expected["uid"]
            or metadata.get("resourceVersion") != expected["resource_version"]
            or hashlib.sha256(canonical(fence_projection(live))).hexdigest() != expected["object_sha256"]
        ):
            raise CleanupError("cleanup fence UID/resourceVersion/spec differs")
        if expected["kind"] == "ConfigMap":
            data = live.get("data")
            if not isinstance(data, dict) or (
                data.get("state") != "reference-data-ready"
                or data.get("authorization_phase") != "cleanup-legacy-resources"
                or data.get("authorization_owner_acknowledged") != "true"
                or data.get("authorization_downstream_acknowledged") != "true"
            ):
                raise CleanupError("cleanup fence ledger is not fully acknowledged")


def validate_live(item: dict[str, str], live: dict[str, object]) -> None:
    metadata = live.get("metadata")
    if not isinstance(metadata, dict):
        raise CleanupError("live object metadata is missing")
    if (
        live.get("apiVersion") != item["api_version"]
        or live.get("kind") != item["kind"]
        or metadata.get("name") != item["name"]
        or metadata.get("namespace") != NAMESPACE
        or metadata.get("uid") != item["uid"]
        or metadata.get("resourceVersion") != item["resource_version"]
        or hashlib.sha256(canonical(live_projection(live))).hexdigest() != item["object_sha256"]
    ):
        raise CleanupError("live object UID/resourceVersion/spec differs from the approved manifest")
    labels = metadata.get("labels") or {}
    if not isinstance(labels, dict):
        raise CleanupError("live object labels are malformed")
    if item["kind"] in {"ServiceAccount", "DaemonSet"}:
        if labels.get("app.kubernetes.io/managed-by") != "fs2-model-controller":
            raise CleanupError("SA/DaemonSet is not a legacy model-controller object")
    else:
        if labels.get("app.kubernetes.io/part-of") != "fs2-serve":
            raise CleanupError("NetworkPolicy is not a legacy FS2 policy")
        if labels.get("app.kubernetes.io/managed-by") == "terraform" or item[
            "name"
        ].startswith("fs2-network-profile-"):
            raise CleanupError("Terraform-owned finite profile policies may never be cleaned by this tool")


def service_account_references(client: Kubectl, name: str) -> list[str]:
    references: list[str] = []
    collections = (
        ("Pod", f"/api/v1/namespaces/{NAMESPACE}/pods", ("spec",)),
        ("PodTemplate", f"/api/v1/namespaces/{NAMESPACE}/podtemplates", ("template", "spec")),
        ("Deployment", f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments", ("spec", "template", "spec")),
        ("StatefulSet", f"/apis/apps/v1/namespaces/{NAMESPACE}/statefulsets", ("spec", "template", "spec")),
        ("DaemonSet", f"/apis/apps/v1/namespaces/{NAMESPACE}/daemonsets", ("spec", "template", "spec")),
        ("ReplicaSet", f"/apis/apps/v1/namespaces/{NAMESPACE}/replicasets", ("spec", "template", "spec")),
        (
            "ReplicationController",
            f"/api/v1/namespaces/{NAMESPACE}/replicationcontrollers",
            ("spec", "template", "spec"),
        ),
        ("Job", f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs", ("spec", "template", "spec")),
        (
            "CronJob",
            f"/apis/batch/v1/namespaces/{NAMESPACE}/cronjobs",
            ("spec", "jobTemplate", "spec", "template", "spec"),
        ),
    )
    for kind, uri, path in collections:
        collection = client.raw(uri)
        if collection is None:  # pragma: no cover - non-optional reads never return None
            raise CleanupError(f"consumer inventory disappeared for {kind}")
        for item in collection.get("items", []):
            value: object = item
            for component in path:
                value = value.get(component, {}) if isinstance(value, dict) else {}
            if isinstance(value, dict) and value.get("serviceAccountName", "default") == name:
                references.append(f"{kind}/{item['metadata']['name']}")
    jobsets = client.raw(
        f"/apis/jobset.x-k8s.io/v1alpha2/namespaces/{NAMESPACE}/jobsets",
        allow_absent=True,
    )
    if jobsets is not None:
        for item in jobsets.get("items", []):
            for replicated_job in item.get("spec", {}).get("replicatedJobs", []) or []:
                pod_spec = replicated_job.get("template", {}).get("spec", {}).get("template", {}).get("spec", {})
                if isinstance(pod_spec, dict) and pod_spec.get("serviceAccountName", "default") == name:
                    references.append(f"JobSet/{item['metadata']['name']}")
    model_deployments = client.raw(
        f"/apis/inference.fs2.nebius.ai/v1alpha1/namespaces/{NAMESPACE}/modeldeployments",
        allow_absent=True,
    )
    if model_deployments is not None:
        for item in model_deployments.get("items", []):
            if name in nested_service_account_names(item.get("spec", {})):
                references.append(f"ModelDeployment/{item['metadata']['name']}")
    return references


def nested_service_account_names(value: object) -> set[str]:
    """Find serviceAccountName in every declared custom-controller template."""

    result: set[str] = set()
    if isinstance(value, dict):
        service_account = value.get("serviceAccountName")
        if isinstance(service_account, str) and service_account:
            result.add(service_account)
        for child in value.values():
            result.update(nested_service_account_names(child))
    elif isinstance(value, list):
        for child in value:
            result.update(nested_service_account_names(child))
    return result


def run(args: argparse.Namespace) -> dict[str, object]:
    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest, objects = validate_manifest(payload)
    digest = hashlib.sha256(canonical(manifest)).hexdigest()
    client = Kubectl(args.kubeconfig, args.context)
    kube_system = client.raw("/api/v1/namespaces/kube-system")
    if kube_system is None:  # pragma: no cover - mandatory object
        raise CleanupError("kube-system namespace is absent")
    if kube_system.get("metadata", {}).get("uid") != manifest["kube_system_uid"]:
        raise CleanupError("selected cluster kube-system UID differs from the cleanup manifest")

    validate_cleanup_fence(client, manifest)
    checked: list[dict[str, str]] = []
    for item in objects:
        uri = path_for(item["kind"], item["name"])
        live = client.raw(uri)
        if live is None:  # pragma: no cover - mandatory object
            raise CleanupError(f"cleanup object disappeared before validation: {item['kind']}/{item['name']}")
        validate_live(item, live)
        checked.append(item)
    if checked:
        retained = ", ".join(f"{item['kind']}/{item['name']}" for item in checked)
        raise CleanupError(
            "no-delete closure is blocked by retained legacy objects; "
            f"no mutation was attempted: {retained}"
        )
    result = {
        "schema": RESULT_SCHEMA,
        "mode": "no-delete-closure",
        "manifest_sha256": digest,
        "baseline_artifact_sha256": manifest["baseline_artifact_sha256"],
        "prior_inventory_sha256": manifest["prior_inventory_sha256"],
        "cluster_id": manifest["cluster_id"],
        "run_id": manifest["run_id"],
        "kube_system_uid": manifest["kube_system_uid"],
        "fence_objects": manifest["fence_objects"],
        "checked_objects": checked,
        "retained_objects": [],
        "removed_objects": [],
    }
    result["result_sha256"] = hashlib.sha256(canonical(result)).hexdigest()
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--manifest", required=True, type=Path)
    result.add_argument("--kubeconfig", required=True, type=Path)
    result.add_argument("--context", required=True)
    return result


def main() -> int:
    try:
        result = run(parser().parse_args())
    except (CleanupError, ProjectionError, OSError, json.JSONDecodeError) as error:
        print(f"SAI-07 no-delete closure refused: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
