#!/usr/bin/env python3
"""Prove an inert retained SAI-07 legacy quarantine without mutation.

The hard no-delete contract forbids this tool from mutating Kubernetes. It can
close a non-empty inventory only when every retained object is already inert:
NetworkPolicies are deny-only, ServiceAccounts are tokenless and unreferenced,
and DaemonSets own no Pods and schedule nothing. The exact admission fence then
freezes those identities, denies new ServiceAccount consumers, and denies Pods
owned by a retained DaemonSet. Anything active or permissive fails closed.
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

SCHEMA = "fs2-serve.nebius.ai/sai07-retained-quarantine/v5"
RESULT_SCHEMA = "fs2-serve.nebius.ai/sai07-retained-quarantine-result/v5"
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


def daemonset_owned_pods(client: Kubectl, name: str, uid: str) -> list[str]:
    collection = client.raw(f"/api/v1/namespaces/{NAMESPACE}/pods")
    if collection is None:  # pragma: no cover - mandatory read
        raise CleanupError("Pod inventory disappeared during retained quarantine verification")
    owned: list[str] = []
    for pod in collection.get("items", []):
        metadata = pod.get("metadata", {}) if isinstance(pod, dict) else {}
        owners = metadata.get("ownerReferences", []) if isinstance(metadata, dict) else []
        if any(
            isinstance(owner, dict)
            and owner.get("apiVersion") == "apps/v1"
            and owner.get("kind") == "DaemonSet"
            and owner.get("name") == name
            and owner.get("uid") == uid
            for owner in owners or []
        ):
            owned.append(str(metadata.get("name", "")))
    return sorted(owned)


def validate_quarantined_object(
    client: Kubectl,
    item: dict[str, str],
    live: dict[str, object],
    retained_daemonsets: frozenset[str] = frozenset(),
) -> None:
    if item["kind"] == "NetworkPolicy":
        spec = live.get("spec")
        if not isinstance(spec, dict) or set(spec.get("policyTypes", [])) != {"Ingress", "Egress"}:
            raise CleanupError("retained NetworkPolicy is not an ingress-and-egress quarantine")
        if spec.get("ingress", []) != [] or spec.get("egress", []) != []:
            raise CleanupError("retained NetworkPolicy grants traffic and cannot be quarantined")
        if not isinstance(spec.get("podSelector"), dict):
            raise CleanupError("retained NetworkPolicy pod selector is malformed")
        return
    if item["kind"] == "ServiceAccount":
        if (
            live.get("automountServiceAccountToken") is not False
            or live.get("secrets", []) != []
            or live.get("imagePullSecrets", []) != []
        ):
            raise CleanupError("retained ServiceAccount is not tokenless and reference-free")
        references = [
            reference
            for reference in service_account_references(client, item["name"])
            if not (
                reference.startswith("DaemonSet/")
                and reference.removeprefix("DaemonSet/") in retained_daemonsets
            )
        ]
        if references:
            raise CleanupError(
                "retained ServiceAccount still has workload consumers: " + ", ".join(references)
            )
        return
    status = live.get("status", {})
    if not isinstance(status, dict):
        raise CleanupError("retained DaemonSet status is malformed")
    counters = (
        "desiredNumberScheduled",
        "currentNumberScheduled",
        "numberReady",
        "numberAvailable",
        "updatedNumberScheduled",
        "numberMisscheduled",
    )
    if any(isinstance(status.get(field, 0), bool) or status.get(field, 0) != 0 for field in counters):
        raise CleanupError("retained DaemonSet still schedules or owns live capacity")
    owned = daemonset_owned_pods(client, item["name"], item["uid"])
    if owned:
        raise CleanupError("retained DaemonSet still owns Pods: " + ", ".join(owned))


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
    retained_daemonsets = frozenset(item["name"] for item in objects if item["kind"] == "DaemonSet")
    for item in objects:
        uri = path_for(item["kind"], item["name"])
        live = client.raw(uri)
        if live is None:  # pragma: no cover - mandatory object
            raise CleanupError(f"cleanup object disappeared before validation: {item['kind']}/{item['name']}")
        validate_live(item, live)
        validate_quarantined_object(client, item, live, retained_daemonsets)
        checked.append(item)
    result = {
        "schema": RESULT_SCHEMA,
        "mode": "retained-quarantine",
        "manifest_sha256": digest,
        "baseline_artifact_sha256": manifest["baseline_artifact_sha256"],
        "prior_inventory_sha256": manifest["prior_inventory_sha256"],
        "cluster_id": manifest["cluster_id"],
        "run_id": manifest["run_id"],
        "kube_system_uid": manifest["kube_system_uid"],
        "fence_objects": manifest["fence_objects"],
        "checked_objects": checked,
        "retained_objects": checked,
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
        print(f"SAI-07 retained quarantine refused: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
