#!/usr/bin/env python3
"""Read-only post-attachment gate for the SAI-26 Grafana route.

The script intentionally issues only ``kubectl get`` calls. It prints no
resource bodies, cookies, credentials, or customer data. Terraform records a
successful invocation as the attachment receipt.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


class GateError(RuntimeError):
    pass


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise GateError(f"{name} is required")
    return value


def bounded_integer(name: str, *, minimum: int, maximum: int) -> int:
    try:
        value = int(required_environment(name))
    except ValueError as error:
        raise GateError(f"{name} must be an integer") from error
    if value < minimum or value > maximum:
        raise GateError(f"{name} is outside the permitted bound")
    return value


def kubectl_json(
    *,
    kubeconfig: Path,
    context: str,
    kind: str,
    name: str,
    namespace: str | None = None,
) -> Mapping[str, Any]:
    command = [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        context,
        "get",
        kind,
        name,
    ]
    if namespace is not None:
        command.extend(["--namespace", namespace])
    command.extend(["--output", "json"])
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        raise GateError(f"read-only lookup failed for {kind}/{name}")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise GateError(f"{kind}/{name} did not return JSON") from error
    if not isinstance(document, Mapping):
        raise GateError(f"{kind}/{name} returned an invalid document")
    return document


def current_generation_conditions(
    resource: Mapping[str, Any],
    *,
    expected_ancestor: Mapping[str, Any],
) -> Sequence[Mapping[str, Any]]:
    generation = resource.get("metadata", {}).get("generation")
    conditions: list[Mapping[str, Any]] = []
    for ancestor in resource.get("status", {}).get("ancestors", []):
        if not isinstance(ancestor, Mapping):
            continue
        ancestor_ref = ancestor.get("ancestorRef", {})
        if not isinstance(ancestor_ref, Mapping) or not (
            ancestor_ref.get("group") == expected_ancestor["group"]
            and ancestor_ref.get("kind") == expected_ancestor["kind"]
            and ancestor_ref.get("name") == expected_ancestor["name"]
            and ancestor_ref.get("namespace", expected_ancestor["namespace"])
            == expected_ancestor["namespace"]
            and ancestor_ref.get("sectionName", expected_ancestor["sectionName"])
            == expected_ancestor["sectionName"]
        ):
            continue
        for condition in ancestor.get("conditions", []):
            if (
                isinstance(condition, Mapping)
                and condition.get("observedGeneration") == generation
            ):
                conditions.append(condition)
    return conditions


def conditions_are_ready(conditions: Sequence[Mapping[str, Any]]) -> bool:
    return all(
        any(
            condition.get("type") == condition_type
            and condition.get("status") == "True"
            for condition in conditions
        )
        for condition_type in ("Accepted", "ResolvedRefs")
    )


def policy_is_ready(
    resource: Mapping[str, Any],
    *,
    namespace: str,
    gateway_name: str,
    listener_name: str,
) -> bool:
    expected_ancestor = {
        "group": "gateway.networking.k8s.io",
        "kind": "Gateway",
        "name": gateway_name,
        "namespace": namespace,
        "sectionName": listener_name,
    }
    return any(
        condition.get("type") == "Accepted" and condition.get("status") == "True"
        for condition in current_generation_conditions(
            resource,
            expected_ancestor=expected_ancestor,
        )
    )


def route_is_ready(
    resource: Mapping[str, Any],
    *,
    namespace: str,
    gateway_name: str,
    listener_name: str,
) -> bool:
    expected_parent = {
        "group": "gateway.networking.k8s.io",
        "kind": "Gateway",
        "name": gateway_name,
        "namespace": namespace,
        "sectionName": listener_name,
    }
    if resource.get("spec", {}).get("parentRefs") != [expected_parent]:
        return False
    generation = resource.get("metadata", {}).get("generation")
    for parent in resource.get("status", {}).get("parents", []):
        if parent.get("parentRef") != expected_parent:
            continue
        conditions = [
            condition
            for condition in parent.get("conditions", [])
            if isinstance(condition, Mapping)
            and condition.get("observedGeneration") == generation
        ]
        if conditions_are_ready(conditions):
            return True
    return False


def route_backends_are_exact(
    resource: Mapping[str, Any],
    *,
    grafana_namespace: str,
    grafana_service: str,
    grafana_port: int,
) -> bool:
    root = "/admin/observability/grafana"
    backend = {
        "group": "",
        "kind": "Service",
        "name": grafana_service,
        "namespace": grafana_namespace,
        "port": grafana_port,
        "weight": 1,
    }
    return resource.get("spec", {}).get("rules") == [
        {
            "name": "grafana-login",
            "matches": [{"path": {"type": "Exact", "value": f"{root}/login"}}],
            "backendRefs": [backend],
        },
        {
            "name": "grafana",
            "matches": [{"path": {"type": "PathPrefix", "value": root}}],
            "backendRefs": [backend],
        },
    ]


def security_policy_is_exact(
    resource: Mapping[str, Any], *, route_name: str
) -> bool:
    spec = resource.get("spec", {})
    expected_target = [
        {
            "group": "gateway.networking.k8s.io",
            "kind": "HTTPRoute",
            "name": route_name,
        }
    ]
    ext_auth = spec.get("extAuth", {})
    http = ext_auth.get("http", {})
    return (
        spec.get("targetRefs") == expected_target
        and ext_auth.get("failOpen") is False
        and ext_auth.get("statusOnError") == 403
        and ext_auth.get("timeout") == "2s"
        and ext_auth.get("headersToExtAuth") == ["cookie"]
        and http.get("pathOverride") == "/admin/api/v1/grafana-authorization"
        and http.get("backendRefs")
        == [
            {
                "group": "",
                "kind": "Service",
                "name": "fs2-serve-control-plane",
                "port": 8080,
            }
        ]
        and "authorization" not in spec
    )


def rate_policy_is_exact(resource: Mapping[str, Any], *, route_name: str) -> bool:
    spec = resource.get("spec", {})
    return (
        spec.get("mergeType") == "StrategicMerge"
        and spec.get("targetRefs")
        == [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "name": route_name,
                "sectionName": "grafana-login",
            }
        ]
        and spec.get("rateLimit", {}).get("local", {}).get("rules")
        == [{"limit": {"requests": 5, "unit": "Minute"}}]
    )


def deny_filter_is_exact(resource: Mapping[str, Any]) -> bool:
    return resource.get("spec") == {
        "directResponse": {
            "contentType": "text/plain",
            "statusCode": 403,
            "body": {"type": "Inline", "inline": "Forbidden"},
        }
    }


def reference_grant_is_exact(
    resource: Mapping[str, Any],
    *,
    route_namespace: str,
    grafana_service: str,
) -> bool:
    return resource.get("spec") == {
        "from": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "namespace": route_namespace,
            }
        ],
        "to": [{"group": "", "kind": "Service", "name": grafana_service}],
    }


def main() -> int:
    try:
        kubeconfig = Path(required_environment("FS2_GATE_KUBECONFIG"))
        context = required_environment("FS2_GATE_KUBE_CONTEXT")
        cluster_id = required_environment("FS2_GATE_CLUSTER_ID")
        cluster_name = required_environment("FS2_GATE_CLUSTER_NAME")
        expected_kube_system_uid = required_environment("FS2_GATE_KUBE_SYSTEM_UID")
        namespace = required_environment("FS2_GATE_NAMESPACE")
        route_name = required_environment("FS2_GATE_ROUTE_NAME")
        gateway_name = required_environment("FS2_GATE_GATEWAY_NAME")
        listener_name = required_environment("FS2_GATE_LISTENER_NAME")
        grafana_namespace = required_environment("FS2_GATE_GRAFANA_NAMESPACE")
        grafana_service = required_environment("FS2_GATE_GRAFANA_SERVICE")
        grafana_port = bounded_integer(
            "FS2_GATE_GRAFANA_PORT", minimum=1, maximum=65535
        )
        timeout_seconds = bounded_integer(
            "FS2_GATE_TIMEOUT_SECONDS", minimum=30, maximum=600
        )
        retry_seconds = bounded_integer(
            "FS2_GATE_RETRY_SECONDS", minimum=1, maximum=10
        )
        if not kubeconfig.is_absolute() or ".." in kubeconfig.parts:
            raise GateError("FS2_GATE_KUBECONFIG must be an absolute bounded path")
        if context != cluster_name or cluster_id not in kubeconfig.read_text(
            encoding="utf-8"
        ):
            raise GateError("kubeconfig context or cluster identity is outside the task contract")

        kube_system = kubectl_json(
            kubeconfig=kubeconfig,
            context=context,
            kind="namespace",
            name="kube-system",
        )
        if kube_system.get("metadata", {}).get("uid") != expected_kube_system_uid:
            raise GateError("kube-system UID does not match the retained cluster receipt")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                route = kubectl_json(
                    kubeconfig=kubeconfig,
                    context=context,
                    kind="httproute.gateway.networking.k8s.io",
                    name=route_name,
                    namespace=namespace,
                )
                security_policy = kubectl_json(
                    kubeconfig=kubeconfig,
                    context=context,
                    kind="securitypolicy.gateway.envoyproxy.io",
                    name="fs2-admin-grafana-client-cidrs",
                    namespace=namespace,
                )
                deny_filter = kubectl_json(
                    kubeconfig=kubeconfig,
                    context=context,
                    kind="httproutefilter.gateway.envoyproxy.io",
                    name="fs2-admin-grafana-prepare-deny",
                    namespace=namespace,
                )
                reference_grant = kubectl_json(
                    kubeconfig=kubeconfig,
                    context=context,
                    kind="referencegrant.gateway.networking.k8s.io",
                    name=route_name,
                    namespace=grafana_namespace,
                )
                rate_policy = kubectl_json(
                    kubeconfig=kubeconfig,
                    context=context,
                    kind="backendtrafficpolicy.gateway.envoyproxy.io",
                    name="fs2-admin-grafana-edge-limit",
                    namespace=namespace,
                )
                if (
                    route_is_ready(
                        route,
                        namespace=namespace,
                        gateway_name=gateway_name,
                        listener_name=listener_name,
                    )
                    and route_backends_are_exact(
                        route,
                        grafana_namespace=grafana_namespace,
                        grafana_service=grafana_service,
                        grafana_port=grafana_port,
                    )
                    and security_policy_is_exact(
                        security_policy, route_name=route_name
                    )
                    and policy_is_ready(
                        security_policy,
                        namespace=namespace,
                        gateway_name=gateway_name,
                        listener_name=listener_name,
                    )
                    and deny_filter_is_exact(deny_filter)
                    and reference_grant_is_exact(
                        reference_grant,
                        route_namespace=namespace,
                        grafana_service=grafana_service,
                    )
                    and rate_policy_is_exact(rate_policy, route_name=route_name)
                    and policy_is_ready(
                        rate_policy,
                        namespace=namespace,
                        gateway_name=gateway_name,
                        listener_name=listener_name,
                    )
                ):
                    print(
                        "Grafana admin-session route is Accepted=True/ResolvedRefs=True and both policies are current-generation Accepted=True",
                        flush=True,
                    )
                    return 0
            except GateError:
                pass
            time.sleep(retry_seconds)
        raise GateError("Grafana attachment did not reach the required fail-closed status")
    except (GateError, OSError, subprocess.SubprocessError) as error:
        print(f"Grafana attachment gate failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
