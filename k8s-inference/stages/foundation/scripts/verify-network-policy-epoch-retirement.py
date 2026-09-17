#!/usr/bin/env python3
"""Apply-deferred proof that an epoch rotation retired prior principals."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


class RetirementError(RuntimeError):
    """The post-apply identity boundary is not exact."""


def canonical(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def run(kubeconfig: Path, context: str, *arguments: str) -> str:
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context, *arguments],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise RetirementError("epoch retirement postflight failed closed")
    return result.stdout


def exact_file(path: Path) -> str:
    try:
        metadata = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise RetirementError("epoch credential is unavailable") from error
    if (
        not path.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise RetirementError("epoch credential custody is unsafe")
    return digest


def user_info(kubeconfig: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(run(kubeconfig, context, "auth", "whoami", "-o", "json"))["status"]["userInfo"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise RetirementError("epoch whoami response is incomplete") from error
    return {
        "username": value.get("username"),
        "uid": value.get("uid"),
        "groups": sorted(value.get("groups", [])),
        "extra": {key: sorted(items) for key, items in sorted(value.get("extra", {}).items())},
    }


def cluster_identity(kubeconfig: Path, context: str) -> tuple[str, str]:
    try:
        config = json.loads(run(kubeconfig, context, "config", "view", "--minify", "--raw", "-o", "json"))
        clusters = config["clusters"]
        server = clusters[0]["cluster"]["server"] if len(clusters) == 1 else None
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise RetirementError("epoch cluster identity is incomplete") from error
    uid = run(kubeconfig, context, "get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}").strip()
    if not isinstance(server, str) or not server or not uid:
        raise RetirementError("epoch cluster identity is incomplete")
    return server, uid


def get_json(kubeconfig: Path, context: str, *arguments: str) -> dict[str, Any]:
    try:
        value = json.loads(run(kubeconfig, context, "get", *arguments, "-o", "json"))
    except json.JSONDecodeError as error:
        raise RetirementError("epoch retirement object is not valid JSON") from error
    if not isinstance(value, dict):
        raise RetirementError("epoch retirement object is not exact")
    return value


def binding_evidence(
    value: dict[str, Any],
    *,
    name: str,
    namespace: str,
    role_kind: str,
    role_name: str,
    subjects: list[str],
) -> dict[str, Any]:
    metadata = value.get("metadata", {})
    labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
    identity = {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace", ""),
        "uid": metadata.get("uid"),
        "resourceVersion": metadata.get("resourceVersion"),
    }
    expected_role_ref = {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": role_kind,
        "name": role_name,
    }
    expected_subjects = sorted(
        [
            {"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": subject}
            for subject in subjects
        ],
        key=canonical,
    )
    live_subjects = value.get("subjects")
    if (
        identity["name"] != name
        or identity["namespace"] != namespace
        or not all(
            isinstance(item, str) and item
            for item in (identity["name"], identity["uid"], identity["resourceVersion"])
        )
        or labels.get("fs2.nebius.ai/network-policy-boundary") != "permanent"
        or value.get("roleRef") != expected_role_ref
        or not isinstance(live_subjects, list)
        or sorted(live_subjects, key=canonical) != expected_subjects
    ):
        raise RetirementError("epoch retirement binding state is not exact")
    return {"metadata": identity, "roleRef": value["roleRef"], "subjects": live_subjects}


def auditor_role_evidence(value: dict[str, Any]) -> dict[str, Any]:
    metadata = value.get("metadata", {})
    labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
    identity = {
        "name": metadata.get("name"),
        "uid": metadata.get("uid"),
        "resourceVersion": metadata.get("resourceVersion"),
    }
    expected_rules = [
        {"apiGroups": [""], "resources": ["namespaces", "serviceaccounts"], "verbs": ["get", "list"]},
        {
            "apiGroups": ["authorization.k8s.io"],
            "resources": ["subjectaccessreviews"],
            "verbs": ["create"],
        },
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["roles", "clusterroles"],
            "verbs": ["get", "list"],
        },
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["clusterrolebindings"],
            "resourceNames": [
                "fs2-network-policy-security-owner",
                "fs2-network-policy-security-auditor",
            ],
            "verbs": ["get", "patch", "update"],
        },
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["rolebindings"],
            "resourceNames": [
                "fs2-network-policy-transition",
                "fs2-network-policy-transition-gateway",
                "fs2-network-policy-transition-controller",
            ],
            "verbs": ["get"],
        },
        {
            "apiGroups": [""],
            "resources": ["configmaps"],
            "resourceNames": [
                "fs2-network-policy-transition",
                "fs2-network-policy-boundary-topology",
                "fs2-network-policy-boundary-parameters",
            ],
            "verbs": ["get"],
        },
        {
            "apiGroups": ["coordination.k8s.io"],
            "resources": ["leases"],
            "resourceNames": ["fs2-network-policy-transition"],
            "verbs": ["get"],
        },
        {
            "apiGroups": ["networking.k8s.io"],
            "resources": ["networkpolicies"],
            "resourceNames": [
                "fs2-serve-control-plane-public-envoy-transition-guard",
                "fs2-serve-control-plane-envoy-controller-xds-transition-guard",
                "fs2-serve-control-plane-envoy-default-deny",
            ],
            "verbs": ["get"],
        },
        {
            "apiGroups": ["admissionregistration.k8s.io"],
            "resources": ["validatingadmissionpolicies", "validatingadmissionpolicybindings"],
            "resourceNames": ["fs2-network-policy-boundary"],
            "verbs": ["get"],
        },
    ]
    rules = value.get("rules")
    if (
        identity["name"] != "fs2-network-policy-security-auditor"
        or not all(isinstance(item, str) and item for item in identity.values())
        or labels.get("fs2.nebius.ai/network-policy-boundary") != "permanent"
        or not isinstance(rules, list)
        or sorted(rules, key=canonical) != sorted(expected_rules, key=canonical)
    ):
        raise RetirementError("epoch retirement auditor role is not exact")
    return {"metadata": identity, "rules": rules}


def can_i(kubeconfig: Path, context: str, expected: str, *arguments: str) -> None:
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context, "auth", "can-i", *arguments],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.stdout.strip() != expected or (expected == "yes" and result.returncode != 0):
        raise RetirementError("epoch authority does not match the post-apply contract")


def main() -> int:
    try:
        query = json.load(sys.stdin)
        required = {
            "mode",
            "context",
            "kube_system_uid",
            "identity_epoch",
            "prior_identity_epoch",
            "successor_identity_epoch",
            "preflight_sha256",
            "current_owner_kubeconfig",
            "prior_owner_kubeconfig",
            "prior_bootstrap_kubeconfig",
            "current_owner_username",
            "current_bootstrap_username",
            "prior_owner_username",
            "prior_bootstrap_username",
            "successor_owner_username",
            "successor_bootstrap_username",
            "current_owner_user_info_sha256",
            "prior_owner_user_info_sha256",
            "prior_bootstrap_user_info_sha256",
            "current_owner_kubeconfig_sha256",
            "prior_owner_kubeconfig_sha256",
            "prior_bootstrap_kubeconfig_sha256",
            "gateway_namespace",
            "controller_namespace",
        }
        if not isinstance(query, dict) or set(query) != required or not all(
            isinstance(value, str) for value in query.values()
        ):
            raise RetirementError("epoch retirement query is not exact")
        if query["mode"] != "public":
            print(canonical({"verified": "true", "contract_sha256": hashlib.sha256(b"internal").hexdigest()}))
            return 0
        epochs = [query["prior_identity_epoch"], query["identity_epoch"], query["successor_identity_epoch"]]
        if len(set(epochs)) != 3 or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", epoch) for epoch in epochs
        ):
            raise RetirementError("epoch retirement identities are not disjoint")

        def principal(role: str, epoch: str) -> str:
            return f"fs2-np-{role}-{hashlib.sha256(epoch.encode()).hexdigest()[:16]}"

        if (
            query["current_owner_username"] != principal("security-owner", query["identity_epoch"])
            or query["current_bootstrap_username"]
            != principal("security-bootstrap", query["identity_epoch"])
            or query["prior_owner_username"] != principal("security-owner", query["prior_identity_epoch"])
            or query["prior_bootstrap_username"]
            != principal("security-bootstrap", query["prior_identity_epoch"])
            or query["successor_owner_username"]
            != principal("security-owner", query["successor_identity_epoch"])
            or query["successor_bootstrap_username"]
            != principal("security-bootstrap", query["successor_identity_epoch"])
            or not re.fullmatch(r"[0-9a-f]{64}", query["preflight_sha256"])
        ):
            raise RetirementError("epoch retirement principal binding is invalid")
        current = Path(query["current_owner_kubeconfig"])
        prior_owner = Path(query["prior_owner_kubeconfig"])
        prior_bootstrap = Path(query["prior_bootstrap_kubeconfig"])
        paths = (current, prior_owner, prior_bootstrap)
        if len({str(path) for path in paths}) != 3:
            raise RetirementError("epoch retirement credentials are not disjoint")
        hashes = [exact_file(path) for path in paths]
        if hashes != [
            query["current_owner_kubeconfig_sha256"],
            query["prior_owner_kubeconfig_sha256"],
            query["prior_bootstrap_kubeconfig_sha256"],
        ]:
            raise RetirementError("epoch retirement credential hashes changed")
        identities = [user_info(path, query["context"]) for path in paths]
        if [identity["username"] for identity in identities] != [
            query["current_owner_username"],
            query["prior_owner_username"],
            query["prior_bootstrap_username"],
        ] or [hashlib.sha256(canonical(identity).encode()).hexdigest() for identity in identities] != [
            query["current_owner_user_info_sha256"],
            query["prior_owner_user_info_sha256"],
            query["prior_bootstrap_user_info_sha256"],
        ]:
            raise RetirementError("epoch retirement identity tuples changed")
        clusters = {cluster_identity(path, query["context"]) for path in paths}
        if len(clusters) != 1 or next(iter(clusters))[1] != query["kube_system_uid"]:
            raise RetirementError("epoch retirement credentials target different clusters")
        cluster_resources = (
            "validatingadmissionpolicies.admissionregistration.k8s.io",
            "validatingadmissionpolicybindings.admissionregistration.k8s.io",
        )
        namespaced_resources = (
            (
                "networkpolicies.networking.k8s.io",
                "fs2-serve-control-plane-public-envoy-transition-guard",
                query["gateway_namespace"],
                True,
            ),
            (
                "networkpolicies.networking.k8s.io",
                "fs2-serve-control-plane-envoy-default-deny",
                query["gateway_namespace"],
                True,
            ),
            (
                "networkpolicies.networking.k8s.io",
                "fs2-serve-control-plane-envoy-controller-xds-transition-guard",
                query["controller_namespace"],
                True,
            ),
            ("configmaps", "fs2-network-policy-transition", "fs2-system", True),
            ("configmaps", "fs2-network-policy-boundary-topology", "fs2-system", False),
            ("configmaps", "fs2-network-policy-boundary-parameters", "fs2-system", True),
            ("leases.coordination.k8s.io", "fs2-network-policy-transition", "fs2-system", True),
        )
        for resource in cluster_resources:
            for verb in ("patch", "update"):
                can_i(current, query["context"], "no", verb, resource)
                can_i(current, query["context"], "yes", verb, resource, "--resource-name=fs2-network-policy-boundary")
                for retired in (prior_owner, prior_bootstrap):
                    can_i(retired, query["context"], "no", verb, resource)
                    can_i(
                        retired,
                        query["context"],
                        "no",
                        verb,
                        resource,
                        "--resource-name=fs2-network-policy-boundary",
                    )
            for identity in paths:
                can_i(
                    identity,
                    query["context"],
                    "no",
                    "delete",
                    resource,
                    "--resource-name=fs2-network-policy-boundary",
                )
                can_i(identity, query["context"], "no", "deletecollection", resource)
        for resource, name, namespace, current_mutates in namespaced_resources:
            for verb in ("patch", "update"):
                can_i(current, query["context"], "no", verb, resource, "--namespace", namespace)
                can_i(
                    current,
                    query["context"],
                    "yes" if current_mutates else "no",
                    verb,
                    resource,
                    f"--resource-name={name}",
                    "--namespace",
                    namespace,
                )
                for retired in (prior_owner, prior_bootstrap):
                    can_i(retired, query["context"], "no", verb, resource, "--namespace", namespace)
                    can_i(
                        retired,
                        query["context"],
                        "no",
                        verb,
                        resource,
                        f"--resource-name={name}",
                        "--namespace",
                        namespace,
                    )
            for identity in paths:
                can_i(
                    identity,
                    query["context"],
                    "no",
                    "delete",
                    resource,
                    f"--resource-name={name}",
                    "--namespace",
                    namespace,
                )
                can_i(identity, query["context"], "no", "deletecollection", resource, "--namespace", namespace)
        expected_mutation_subjects = [
            query["current_owner_username"],
            query["successor_owner_username"],
            query["successor_bootstrap_username"],
        ]
        binding_specs = (
            (
                "clusterrolebinding",
                "fs2-network-policy-security-owner",
                "",
                "ClusterRole",
                "fs2-network-policy-security-owner",
                expected_mutation_subjects,
            ),
            (
                "clusterrolebinding",
                "fs2-network-policy-security-auditor",
                "",
                "ClusterRole",
                "fs2-network-policy-security-auditor",
                [query["current_bootstrap_username"], query["successor_bootstrap_username"]],
            ),
            (
                "rolebinding",
                "fs2-network-policy-transition",
                "fs2-system",
                "Role",
                "fs2-network-policy-transition",
                expected_mutation_subjects,
            ),
            (
                "rolebinding",
                "fs2-network-policy-transition-gateway",
                query["gateway_namespace"],
                "Role",
                "fs2-network-policy-transition-gateway",
                expected_mutation_subjects,
            ),
            (
                "rolebinding",
                "fs2-network-policy-transition-controller",
                query["controller_namespace"],
                "Role",
                "fs2-network-policy-transition-controller",
                expected_mutation_subjects,
            ),
        )
        auditor_role = auditor_role_evidence(
            get_json(
                current,
                query["context"],
                "clusterrole",
                "fs2-network-policy-security-auditor",
            )
        )
        bindings = []
        for resource, name, namespace, role_kind, role_name, subjects in binding_specs:
            arguments = [resource, name]
            if namespace:
                arguments.extend(["--namespace", namespace])
            bindings.append(
                binding_evidence(
                    get_json(current, query["context"], *arguments),
                    name=name,
                    namespace=namespace,
                    role_kind=role_kind,
                    role_name=role_name,
                    subjects=subjects,
                )
            )
            authorization_resource = (
                "clusterrolebindings.rbac.authorization.k8s.io"
                if not namespace
                else "rolebindings.rbac.authorization.k8s.io"
            )
            suffix = ("--namespace", namespace) if namespace else ()
            for verb in ("patch", "update"):
                can_i(current, query["context"], "no", verb, authorization_resource, *suffix)
                can_i(
                    current,
                    query["context"],
                    "no",
                    verb,
                    authorization_resource,
                    f"--resource-name={name}",
                    *suffix,
                )
            for retired in (prior_owner, prior_bootstrap):
                for verb in ("patch", "update"):
                    can_i(retired, query["context"], "no", verb, authorization_resource, *suffix)
                    can_i(
                        retired,
                        query["context"],
                        "no",
                        verb,
                        authorization_resource,
                        f"--resource-name={name}",
                        *suffix,
                    )
            for identity in paths:
                can_i(
                    identity,
                    query["context"],
                    "no",
                    "delete",
                    authorization_resource,
                    f"--resource-name={name}",
                    *suffix,
                )
                can_i(identity, query["context"], "no", "deletecollection", authorization_resource, *suffix)
        digest = hashlib.sha256(
            canonical(
                {
                    "query": query,
                    "identities": identities,
                    "clusters": sorted(clusters),
                    "auditor_role": auditor_role,
                    "bindings": bindings,
                }
            ).encode()
        ).hexdigest()
        print(canonical({"verified": "true", "contract_sha256": digest}))
        return 0
    except (OSError, RetirementError, ValueError, json.JSONDecodeError) as error:
        print(f"network-policy epoch retirement failed closed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
