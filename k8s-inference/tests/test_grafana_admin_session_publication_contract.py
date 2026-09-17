from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATUS_GATE_PATH = (
    ROOT
    / "stages"
    / "workloads"
    / "scripts"
    / "wait-for-grafana-admin-session-publication.py"
)


def _status_gate():
    spec = importlib.util.spec_from_file_location(
        "grafana_admin_session_status_gate", STATUS_GATE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _conditions(generation: int) -> list[dict[str, object]]:
    return [
        {
            "type": "Accepted",
            "status": "True",
            "observedGeneration": generation,
        },
        {
            "type": "ResolvedRefs",
            "status": "True",
            "observedGeneration": generation,
        },
    ]


def test_rejected_source_ip_publication_is_statically_disabled() -> None:
    root_variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
    foundation = (ROOT / "stages/foundation/admin_grafana.tf").read_text(
        encoding="utf-8"
    )

    assert "condition     = !var.deployment.observability.grafana.publish_external" in root_variables
    assert "rejected source-IP publication path" in root_variables
    assert "condition     = !var.grafana_publication.enabled" in foundation
    assert "rejected native-login/source-IP path" in foundation


def test_successor_uses_admin_session_ext_auth_without_client_ip_trust() -> None:
    successor = (
        ROOT / "stages/workloads/admin_grafana_admin_session.tf"
    ).read_text(encoding="utf-8")
    compact = "".join(successor.split())

    assert 'headersToExtAuth=["cookie"]' in compact
    assert 'path_override == "/admin/api/v1/grafana-authorization"' in successor
    assert "failOpen=false" in compact
    assert "statusOnError=403" in compact
    assert "clientCIDRs" not in successor
    assert "clientIPDetection" not in successor
    assert "proxyProtocol" not in successor


def test_route_is_quarantined_before_status_gated_backend_attachment() -> None:
    successor = (
        ROOT / "stages/workloads/admin_grafana_admin_session.tf"
    ).read_text(encoding="utf-8")

    assert (
        "parentRefs = [local.grafana_admin_session_parent_ref]"
    ) in successor
    assert "grafana_admin_session_quarantine_rules" in successor
    assert 'kind       = "HTTPRouteFilter"' in successor
    assert 'statusCode  = 403' in successor
    assert 'count       = local.grafana_admin_session_attach ? 1 : 0' in successor
    assert "grafana_admin_session_observed_contract_exact" in successor
    assert "policy_status.security.Accepted" in successor
    assert "policy_status.rate_limit.Accepted" in successor
    assert "grafana_admin_session_observed_route_prepared" in successor
    assert '"Accepted", "ResolvedRefs"' in successor
    assert "grafana_admin_session_attachment_receipt" in successor
    assert successor.count("moved {") == 4


def test_login_limit_is_narrow_and_secondary_to_admin_session() -> None:
    successor = (
        ROOT / "stages/workloads/admin_grafana_admin_session.tf"
    ).read_text(encoding="utf-8")
    compact = "".join(successor.split())

    assert 'grafana_admin_session_login_rule="grafana-login"' in compact
    assert "requests=5" in compact
    assert 'unit="Minute"' in compact
    assert "sectionName = local.grafana_admin_session_login_rule" in successor
    assert 'defense_role = "secondary-to-admin-session"' in successor
    assert "requests = 200" not in successor


def test_status_gate_rejects_stale_or_incomplete_policy_conditions() -> None:
    gate = _status_gate()
    ancestor_ref = {
        "group": "gateway.networking.k8s.io",
        "kind": "Gateway",
        "name": "public",
        "namespace": "fs2-system",
        "sectionName": "public-https",
    }
    ready = {
        "metadata": {"generation": 7},
        "status": {
            "ancestors": [
                {"ancestorRef": ancestor_ref, "conditions": _conditions(7)}
            ]
        },
    }
    stale = {
        "metadata": {"generation": 8},
        "status": {
            "ancestors": [
                {"ancestorRef": ancestor_ref, "conditions": _conditions(7)}
            ]
        },
    }
    incomplete = {
        "metadata": {"generation": 7},
        "status": {
            "ancestors": [
                {"ancestorRef": ancestor_ref, "conditions": [_conditions(7)[1]]}
            ]
        },
    }
    wrong_ancestor = {
        "metadata": {"generation": 7},
        "status": {
            "ancestors": [
                {
                    "ancestorRef": {**ancestor_ref, "name": "unrelated"},
                    "conditions": _conditions(7),
                }
            ]
        },
    }

    arguments = {
        "namespace": "fs2-system",
        "gateway_name": "public",
        "listener_name": "public-https",
    }
    assert gate.policy_is_ready(ready, **arguments)
    assert not gate.policy_is_ready(stale, **arguments)
    assert not gate.policy_is_ready(incomplete, **arguments)
    assert not gate.policy_is_ready(wrong_ancestor, **arguments)


def test_post_attachment_gate_requires_exact_reference_grant() -> None:
    gate = _status_gate()
    grant = {
        "spec": {
            "from": [
                {
                    "group": "gateway.networking.k8s.io",
                    "kind": "HTTPRoute",
                    "namespace": "fs2-system",
                }
            ],
            "to": [
                {
                    "group": "",
                    "kind": "Service",
                    "name": "fs2-r927c465c6d-monitoring-grafana",
                }
            ],
        }
    }

    assert gate.reference_grant_is_exact(
        grant,
        route_namespace="fs2-system",
        grafana_service="fs2-r927c465c6d-monitoring-grafana",
    )
    grant["spec"]["to"][0]["name"] = "unrelated-service"
    assert not gate.reference_grant_is_exact(
        grant,
        route_namespace="fs2-system",
        grafana_service="fs2-r927c465c6d-monitoring-grafana",
    )


def test_prepare_filter_is_an_exact_gateway_native_403() -> None:
    gate = _status_gate()
    exact = {
        "spec": {
            "directResponse": {
                "contentType": "text/plain",
                "statusCode": 403,
                "body": {"type": "Inline", "inline": "Forbidden"},
            }
        }
    }

    assert gate.deny_filter_is_exact(exact)
    exact["spec"]["directResponse"]["statusCode"] = 200
    assert not gate.deny_filter_is_exact(exact)


def test_status_gate_requires_exact_attached_parent_and_current_route_status() -> None:
    gate = _status_gate()
    parent_ref = {
        "group": "gateway.networking.k8s.io",
        "kind": "Gateway",
        "name": "public",
        "namespace": "fs2-system",
        "sectionName": "public-https",
    }
    route = {
        "metadata": {"generation": 4},
        "spec": {"parentRefs": [parent_ref]},
        "status": {
            "parents": [{"parentRef": parent_ref, "conditions": _conditions(4)}]
        },
    }

    assert gate.route_is_ready(
        route,
        namespace="fs2-system",
        gateway_name="public",
        listener_name="public-https",
    )
    route["status"]["parents"][0]["conditions"] = _conditions(3)
    assert not gate.route_is_ready(
        route,
        namespace="fs2-system",
        gateway_name="public",
        listener_name="public-https",
    )
