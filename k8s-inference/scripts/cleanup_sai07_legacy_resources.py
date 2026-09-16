#!/usr/bin/env python3
"""Delete only UID-fenced legacy model-controller objects.

The default is a read-only plan.  Execution additionally requires the SHA-256
of the exact canonical manifest on the command line.  Every DELETE carries a
Kubernetes UID precondition, and ServiceAccounts are refused while any Pod or
controller template still references them.  The resulting UID list belongs in
the signed ``legacy-clean`` transition receipt; this tool is not receipt
authority by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote


SCHEMA = "fs2-serve.nebius.ai/sai07-legacy-cleanup/v1"
NAMESPACE = "fs2-models"
MAX_OBJECTS = 128
KINDS = {
    "NetworkPolicy": ("networking.k8s.io", "v1", "networkpolicies"),
    "ServiceAccount": ("", "v1", "serviceaccounts"),
    "DaemonSet": ("apps", "v1", "daemonsets"),
}


class CleanupError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def path_for(kind: str, name: str) -> str:
    group, version, resource = KINDS[kind]
    prefix = f"/apis/{group}/{version}" if group else f"/api/{version}"
    return f"{prefix}/namespaces/{NAMESPACE}/{resource}/{quote(name, safe='')}"


class Kubectl:
    def __init__(self, kubeconfig: Path, context: str) -> None:
        self.base = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context]

    def raw(self, uri: str) -> dict[str, object]:
        completed = subprocess.run(
            [*self.base, "get", "--raw", uri], check=False, capture_output=True, text=True
        )
        if completed.returncode != 0:
            raise CleanupError(f"read failed for {uri}: {completed.stderr.strip()}")
        return json.loads(completed.stdout)

    def delete_uid(self, uri: str, uid: str) -> None:
        body = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}}
        completed = subprocess.run(
            [*self.base, "delete", "--raw", uri, "-f", "-"],
            input=json.dumps(body),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise CleanupError(f"UID-fenced delete failed for {uri}: {completed.stderr.strip()}")


def validate_manifest(raw: object) -> tuple[dict[str, object], list[dict[str, str]]]:
    if not isinstance(raw, dict) or set(raw) != {"schema", "cluster_id", "run_id", "kube_system_uid", "objects"}:
        raise CleanupError("cleanup manifest fields differ from schema")
    if raw["schema"] != SCHEMA:
        raise CleanupError("cleanup manifest schema is unsupported")
    for field in ("cluster_id", "run_id", "kube_system_uid"):
        if not isinstance(raw[field], str) or not raw[field]:
            raise CleanupError(f"{field} is required")
    objects = raw["objects"]
    if not isinstance(objects, list) or len(objects) > MAX_OBJECTS:
        raise CleanupError(f"objects must be a list of at most {MAX_OBJECTS}")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in objects:
        if not isinstance(item, dict) or set(item) != {"kind", "namespace", "name", "uid"}:
            raise CleanupError("cleanup object fields differ from schema")
        if item["kind"] not in KINDS or item["namespace"] != NAMESPACE:
            raise CleanupError("cleanup object kind or namespace is outside the bounded scope")
        if not all(isinstance(item[key], str) and item[key] for key in ("name", "uid")):
            raise CleanupError("cleanup object name and UID are required")
        identity = (item["kind"], item["name"])
        if identity in seen:
            raise CleanupError("cleanup object identities must be unique")
        seen.add(identity)
        normalized.append(item)
    return raw, normalized


def validate_live(item: dict[str, str], live: dict[str, object]) -> None:
    metadata = live.get("metadata")
    if not isinstance(metadata, dict):
        raise CleanupError("live object metadata is missing")
    if metadata.get("name") != item["name"] or metadata.get("namespace") != NAMESPACE or metadata.get("uid") != item["uid"]:
        raise CleanupError("live object identity or UID differs from the approved manifest")
    labels = metadata.get("labels") or {}
    if not isinstance(labels, dict):
        raise CleanupError("live object labels are malformed")
    if item["kind"] in {"ServiceAccount", "DaemonSet"}:
        if labels.get("app.kubernetes.io/managed-by") != "fs2-model-controller":
            raise CleanupError("SA/DaemonSet is not a legacy model-controller object")
    else:
        if labels.get("app.kubernetes.io/part-of") != "fs2-serve":
            raise CleanupError("NetworkPolicy is not a legacy FS2 policy")
        if labels.get("app.kubernetes.io/managed-by") == "terraform" or item["name"].startswith("fs2-network-profile-"):
            raise CleanupError("Terraform-owned finite profile policies may never be cleaned by this tool")


def service_account_references(client: Kubectl, name: str) -> list[str]:
    references: list[str] = []
    collections = (
        ("Pod", f"/api/v1/namespaces/{NAMESPACE}/pods", ("spec",)),
        ("Deployment", f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments", ("spec", "template", "spec")),
        ("DaemonSet", f"/apis/apps/v1/namespaces/{NAMESPACE}/daemonsets", ("spec", "template", "spec")),
        ("Job", f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs", ("spec", "template", "spec")),
    )
    for kind, uri, path in collections:
        collection = client.raw(uri)
        for item in collection.get("items", []):
            value: object = item
            for component in path:
                value = value.get(component, {}) if isinstance(value, dict) else {}
            if isinstance(value, dict) and value.get("serviceAccountName", "default") == name:
                references.append(f"{kind}/{item['metadata']['name']}")
    return references


def run(args: argparse.Namespace) -> dict[str, object]:
    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest, objects = validate_manifest(payload)
    digest = hashlib.sha256(canonical(manifest)).hexdigest()
    if args.execute and args.approval_sha256 != digest:
        raise CleanupError("--execute requires the exact canonical manifest SHA-256")
    client = Kubectl(args.kubeconfig, args.context)
    kube_system = client.raw("/api/v1/namespaces/kube-system")
    if kube_system.get("metadata", {}).get("uid") != manifest["kube_system_uid"]:
        raise CleanupError("selected cluster kube-system UID differs from the cleanup manifest")

    checked: list[dict[str, str]] = []
    for item in objects:
        uri = path_for(item["kind"], item["name"])
        live = client.raw(uri)
        validate_live(item, live)
        if item["kind"] == "ServiceAccount":
            references = service_account_references(client, item["name"])
            if references:
                raise CleanupError(f"ServiceAccount {item['name']} is still referenced by {', '.join(references)}")
        checked.append(item)
    if args.execute:
        for item in checked:
            client.delete_uid(path_for(item["kind"], item["name"]), item["uid"])
    return {
        "schema": "fs2-serve.nebius.ai/sai07-legacy-cleanup-result/v1",
        "mode": "execute" if args.execute else "plan",
        "manifest_sha256": digest,
        "cluster_id": manifest["cluster_id"],
        "run_id": manifest["run_id"],
        "kube_system_uid": manifest["kube_system_uid"],
        "checked": checked,
        "deleted_uids": [item["uid"] for item in checked] if args.execute else [],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--manifest", required=True, type=Path)
    result.add_argument("--kubeconfig", required=True, type=Path)
    result.add_argument("--context", required=True)
    result.add_argument("--execute", action="store_true")
    result.add_argument("--approval-sha256")
    return result


def main() -> int:
    try:
        result = run(parser().parse_args())
    except (CleanupError, OSError, json.JSONDecodeError) as error:
        print(f"SAI-07 cleanup refused: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
