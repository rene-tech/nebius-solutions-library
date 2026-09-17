#!/usr/bin/env python3
"""Audit one inactive SAI-07 identity without reading Secret or workload data.

Run this separately with the platform identity and with the post-apply inactive
custody-owner identity. The result is a non-secret input to the signed external
handoff; it is not itself rollout authority.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA = "fs2-serve.nebius.ai/sai07-effective-authority-audit/v1"

CLUSTER_CHECKS = {
    "impersonate:v1/users": ("impersonate", "", "users", ""),
    "impersonate:v1/groups": ("impersonate", "", "groups", ""),
    "impersonate:v1/serviceaccounts": ("impersonate", "", "serviceaccounts", ""),
    "impersonate:authentication.k8s.io/uids": ("impersonate", "authentication.k8s.io", "uids", ""),
    "impersonate:authentication.k8s.io/userextras": ("impersonate", "authentication.k8s.io", "userextras", ""),
    "create:authentication.k8s.io/tokenreviews": ("create", "authentication.k8s.io", "tokenreviews", ""),
    "create:authorization.k8s.io/subjectaccessreviews": ("create", "authorization.k8s.io", "subjectaccessreviews", ""),
    "bind:rbac.authorization.k8s.io/roles": ("bind", "rbac.authorization.k8s.io", "roles", ""),
    "bind:rbac.authorization.k8s.io/clusterroles": ("bind", "rbac.authorization.k8s.io", "clusterroles", ""),
    "escalate:rbac.authorization.k8s.io/roles": ("escalate", "rbac.authorization.k8s.io", "roles", ""),
    "escalate:rbac.authorization.k8s.io/clusterroles": ("escalate", "rbac.authorization.k8s.io", "clusterroles", ""),
    "create:rbac.authorization.k8s.io/clusterrolebindings": ("create", "rbac.authorization.k8s.io", "clusterrolebindings", ""),
    "update:rbac.authorization.k8s.io/clusterrolebindings": ("update", "rbac.authorization.k8s.io", "clusterrolebindings", ""),
    "patch:rbac.authorization.k8s.io/clusterrolebindings": ("patch", "rbac.authorization.k8s.io", "clusterrolebindings", ""),
    "create:admissionregistration.k8s.io/validatingadmissionpolicies": ("create", "admissionregistration.k8s.io", "validatingadmissionpolicies", ""),
    "update:admissionregistration.k8s.io/validatingadmissionpolicies": ("update", "admissionregistration.k8s.io", "validatingadmissionpolicies", ""),
    "patch:admissionregistration.k8s.io/validatingadmissionpolicies": ("patch", "admissionregistration.k8s.io", "validatingadmissionpolicies", ""),
    "delete:admissionregistration.k8s.io/validatingadmissionpolicies": ("delete", "admissionregistration.k8s.io", "validatingadmissionpolicies", ""),
    "create:admissionregistration.k8s.io/validatingadmissionpolicybindings": ("create", "admissionregistration.k8s.io", "validatingadmissionpolicybindings", ""),
    "update:admissionregistration.k8s.io/validatingadmissionpolicybindings": ("update", "admissionregistration.k8s.io", "validatingadmissionpolicybindings", ""),
    "patch:admissionregistration.k8s.io/validatingadmissionpolicybindings": ("patch", "admissionregistration.k8s.io", "validatingadmissionpolicybindings", ""),
    "delete:admissionregistration.k8s.io/validatingadmissionpolicybindings": ("delete", "admissionregistration.k8s.io", "validatingadmissionpolicybindings", ""),
}
NAMESPACED_CHECKS = {
    "create:rbac.authorization.k8s.io/rolebindings": ("create", "rbac.authorization.k8s.io", "rolebindings", ""),
    "update:rbac.authorization.k8s.io/rolebindings": ("update", "rbac.authorization.k8s.io", "rolebindings", ""),
    "patch:rbac.authorization.k8s.io/rolebindings": ("patch", "rbac.authorization.k8s.io", "rolebindings", ""),
    "get:v1/secrets": ("get", "", "secrets", ""),
    "list:v1/secrets": ("list", "", "secrets", ""),
    "watch:v1/secrets": ("watch", "", "secrets", ""),
    "create:v1/serviceaccounts/token": ("create", "", "serviceaccounts", "token"),
    "create:v1/pods/proxy": ("create", "", "pods", "proxy"),
    "get:v1/pods/proxy": ("get", "", "pods", "proxy"),
    "create:v1/services/proxy": ("create", "", "services", "proxy"),
    "get:v1/services/proxy": ("get", "", "services", "proxy"),
}
NAMED_LEDGER_CHECKS = {
    "update:v1/configmaps/fs2-system/fs2-pod-security-rollout-ledger": "update",
    "patch:v1/configmaps/fs2-system/fs2-pod-security-rollout-ledger": "patch",
    "delete:v1/configmaps/fs2-system/fs2-pod-security-rollout-ledger": "delete",
}


class AuditError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class Client:
    def __init__(self, kubeconfig: Path, context: str) -> None:
        if not kubeconfig.is_absolute() or ".." in kubeconfig.parts:
            raise AuditError("kubeconfig must be absolute without parent traversal")
        self.base = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context]

    def raw(self, path: str) -> dict[str, Any]:
        completed = subprocess.run(
            [*self.base, "get", "--raw", path], check=False, capture_output=True, text=True, timeout=30
        )
        if completed.returncode != 0:
            raise AuditError(f"metadata read failed for {path}")
        value = json.loads(completed.stdout)
        if not isinstance(value, dict):
            raise AuditError("Kubernetes response is not an object")
        return value

    def review(self, path: str, value: dict[str, Any]) -> dict[str, Any]:
        completed = subprocess.run(
            [*self.base, "create", "--raw", path, "-f", "-"],
            input=canonical(value),
            check=False,
            capture_output=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise AuditError("authorization review failed")
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise AuditError("authorization review response is not an object")
        return result

    def allowed(
        self,
        verb: str,
        group: str,
        resource: str,
        namespace: str,
        subresource: str = "",
        name: str = "",
    ) -> bool:
        attributes = {
            "verb": verb,
            "group": group,
            "resource": resource,
            **({"namespace": namespace} if namespace else {}),
            **({"subresource": subresource} if subresource else {}),
            **({"name": name} if name else {}),
        }
        review = self.review(
            "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
            {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectAccessReview",
                "spec": {"resourceAttributes": attributes},
            },
        )
        status = review.get("status")
        return isinstance(status, dict) and status.get("allowed") is True


def run(args: argparse.Namespace) -> dict[str, Any]:
    client = Client(args.kubeconfig, args.context)
    identity = client.review(
        "/apis/authentication.k8s.io/v1beta1/selfsubjectreviews",
        {"apiVersion": "authentication.k8s.io/v1beta1", "kind": "SelfSubjectReview", "spec": {}},
    ).get("status", {}).get("userInfo", {})
    username = identity.get("username") if isinstance(identity, dict) else None
    groups = sorted(identity.get("groups", [])) if isinstance(identity, dict) else []
    if username != args.expected_username or args.expected_group not in groups or "system:masters" in groups:
        raise AuditError("authenticated identity differs from the bounded external subject")

    namespace_collection = client.raw("/api/v1/namespaces")
    namespaces = sorted(
        item.get("metadata", {}).get("name", "") for item in namespace_collection.get("items", [])
    )
    if not namespaces or any(not namespace for namespace in namespaces):
        raise AuditError("complete namespace inventory is unavailable")

    reviews: list[dict[str, Any]] = []
    denied: list[str] = []
    for check_id, (verb, group, resource, subresource) in sorted(CLUSTER_CHECKS.items()):
        allowed = client.allowed(verb, group, resource, "", subresource)
        reviews.append({"check": check_id, "namespace": "", "allowed": allowed})
        if allowed:
            raise AuditError(f"identity retains forbidden cluster authority: {check_id}")
        denied.append(check_id)
    for check_id, (verb, group, resource, subresource) in sorted(NAMESPACED_CHECKS.items()):
        for namespace in namespaces:
            allowed = client.allowed(verb, group, resource, namespace, subresource)
            reviews.append({"check": check_id, "namespace": namespace, "allowed": allowed})
            if allowed:
                raise AuditError(f"identity retains forbidden authority in {namespace}: {check_id}")
        denied.append(check_id)
    for check_id, verb in sorted(NAMED_LEDGER_CHECKS.items()):
        allowed = client.allowed(
            verb, "", "configmaps", "fs2-system", name="fs2-pod-security-rollout-ledger"
        )
        reviews.append({"check": check_id, "namespace": "fs2-system", "allowed": allowed})
        if allowed:
            raise AuditError(f"identity retains forbidden ledger authority: {check_id}")
        denied.append(check_id)

    rules: list[dict[str, Any]] = []
    for namespace in namespaces:
        review = client.review(
            "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews",
            {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectRulesReview",
                "spec": {"namespace": namespace},
            },
        )
        status = review.get("status")
        if not isinstance(status, dict) or status.get("incomplete") is not False:
            raise AuditError("SelfSubjectRulesReview is incomplete")
        rules.append({"namespace": namespace, "status": status})
    kube_system = client.raw("/api/v1/namespaces/kube-system")
    kube_system_uid = kube_system.get("metadata", {}).get("uid")
    if kube_system_uid != args.kube_system_uid:
        raise AuditError("selected cluster identity differs")
    return {
        "schema": SCHEMA,
        "username": username,
        "groups": groups,
        "cluster_id": args.cluster_id,
        "kube_system_uid": kube_system_uid,
        "namespace_inventory_sha256": hashlib.sha256(canonical(namespaces)).hexdigest(),
        "namespace_count": len(namespaces),
        "self_subject_rules_review_sha256": hashlib.sha256(canonical(rules)).hexdigest(),
        "self_subject_access_review_sha256": hashlib.sha256(canonical(reviews)).hexdigest(),
        "denied_checks": sorted(denied),
        "observed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--kubeconfig", required=True, type=Path)
    result.add_argument("--context", required=True)
    result.add_argument("--cluster-id", required=True)
    result.add_argument("--kube-system-uid", required=True)
    result.add_argument("--expected-username", required=True)
    result.add_argument("--expected-group", required=True)
    return result


def main() -> int:
    try:
        result = run(parser().parse_args())
    except (AuditError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"SAI-07 effective-authority audit rejected: {error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
