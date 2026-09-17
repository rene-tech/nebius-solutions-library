from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fs2_serve.provider_custody_gateway import (
    GatewayPolicy,
    Principal,
    ProviderCustodyError,
    ProviderCustodyGateway,
)

TRANSITION_ID = "spiffe://provider.example/model-network/transition"
TRANSITION_USER = "fs2-model-network-transition"
TRANSITION_LEASE = (
    "leases.coordination.k8s.io/fs2-system/fs2-model-network-transition"
)
FROZEN_POLICY = (
    "validatingadmissionpolicies.admissionregistration.k8s.io/"
    "_cluster/fs2-model-network-static-custody"
)


def test_gateway_identity_is_a_verified_transport_digest_not_an_http_header() -> None:
    entrypoint = (
        Path(__file__).parents[1]
        / "src/fs2_serve/provider_custody_entrypoint.py"
    ).read_text()
    gateway_source = (
        Path(__file__).parents[1] / "src/fs2_serve/provider_custody_gateway.py"
    ).read_text()
    exporter_source = (
        Path(__file__).parents[1] / "src/fs2_serve/provider_authority_exporter.py"
    ).read_text()

    assert 'get_extra_info("ssl_object")' in entrypoint
    assert "getpeercert(binary_form=True)" in entrypoint
    assert "ssl_cert_reqs=ssl.CERT_REQUIRED" in entrypoint
    assert "ssl.TLSVersion.TLSv1_3" in entrypoint
    assert 'policy.member["listener_address"]' in entrypoint
    assert 'policy.member["listener_port"]' in entrypoint
    assert "x_fs2_provider_client_certificate" not in gateway_source
    assert "Header(" not in gateway_source
    assert "FS2_PROVIDER_AUTHORITY_API_SERVER_CERT_SHA256" in exporter_source
    assert "http.client.HTTPSConnection" in exporter_source
    assert "connection.sock.getpeercert(binary_form=True)" in exporter_source
    assert "connection.request(" in exporter_source
    assert "ssl.TLSVersion.TLSv1_3" in exporter_source
    assert "provider-control-plane-authority-observation/v1" in exporter_source


class StubClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes, object]] = []

    async def request(
        self, method: str, target: str, *, content: bytes, headers: object
    ) -> httpx.Response:
        self.requests.append((method, target, content, headers))
        return httpx.Response(200, json={"kind": "Lease"})


def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ProviderCustodyGateway, StubClient]:
    token = tmp_path / "upstream-token"
    token.write_text("provider-gateway-upstream-token", encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.setenv("FS2_PROVIDER_CUSTODY_UPSTREAM_TOKEN", str(token))
    principals = {
        str(index) * 64: Principal(
            TRANSITION_ID if index == 1 else f"spiffe://provider.example/model-network/p{index}",
            TRANSITION_USER if index == 1 else f"fs2-model-network-p{index}",
            (
                "fs2:model-network-transition"
                if index == 1
                else f"fs2:model-network-p{index}",
                "system:authenticated",
            ),
            index == 2,
        )
        for index in range(1, 7)
    }
    value: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/model-network-provider-gateway-policy/v3",
        "cluster_id": "mk8scluster-test",
        "policy_id": "network-boundary-test",
        "policy_revision": "1",
        "cluster_resource_version": 7,
        "upstream_api_url": "https://cluster.example.test",
        "direct_control_plane_access": (
            "provider-firewall-exact-hosts-plus-excluded-guard-rbac-closure"
        ),
        "gateway_members": [],
        "provider_inventory_sha256": "7" * 64,
        "kubernetes_authorization_sha256": "8" * 64,
        "provider_authority_census_sha256": "4" * 64,
        "gateway_runtime_measurements_sha256": "5" * 64,
        "control_plane_allowed_cidrs": ["192.0.2.10/32", "192.0.2.11/32"],
        "principals": {},
        "mutation_freeze": {
            "active_from": "2026-09-17T00:00:00Z",
            "active_until": "2999-09-17T00:00:00Z",
            "protected_kubernetes_resources": [FROZEN_POLICY],
        },
        "operation_locks": {
            "locks": {
                TRANSITION_LEASE: {
                    "principal_id": TRANSITION_ID,
                    "operations": ["PATCH"],
                }
            }
        },
    }
    member = {
        "member_id": "gateway-a",
        "status_url": "https://192.0.2.10/v1/custody/status",
        "host_cidr": "192.0.2.10/32",
        "listener_address": "10.0.0.10",
        "listener_port": 8443,
        "server_certificate_sha256": "9" * 64,
        "iam_principal_id": "serviceaccount-gateway-a",
        "instance": {"id": "instance-a", "resource_version": 1, "semantic_sha256": "a" * 64},
        "security_group": {"id": "security-group-a", "resource_version": 1, "semantic_sha256": "b" * 64},
        "security_rules": [{"id": "security-rule-a", "resource_version": 1, "semantic_sha256": "c" * 64}],
        "access_permits": [{"id": "access-permit-a", "resource_version": 1, "semantic_sha256": "d" * 64}],
    }
    value["gateway_members"] = [
        member,
        {
            **member,
            "member_id": "gateway-b",
            "status_url": "https://192.0.2.11/v1/custody/status",
            "host_cidr": "192.0.2.11/32",
            "listener_address": "10.0.0.11",
            "listener_port": 8443,
            "server_certificate_sha256": "e" * 64,
            "iam_principal_id": "serviceaccount-gateway-b",
            "instance": {"id": "instance-b", "resource_version": 1, "semantic_sha256": "f" * 64},
            "security_group": {"id": "security-group-b", "resource_version": 1, "semantic_sha256": "1" * 64},
            "security_rules": [{"id": "security-rule-b", "resource_version": 1, "semantic_sha256": "2" * 64}],
            "access_permits": [{"id": "access-permit-b", "resource_version": 1, "semantic_sha256": "3" * 64}],
        },
    ]
    client = StubClient()
    policy = GatewayPolicy(
        "a" * 64,
        value,
        principals,
        frozenset({FROZEN_POLICY}),
        (
            "clusterrolebindings.rbac.authorization.k8s.io/_cluster/",
            "clusterroles.rbac.authorization.k8s.io/_cluster/",
            "rolebindings.rbac.authorization.k8s.io/",
            "roles.rbac.authorization.k8s.io/",
        ),
        "gateway-a",
        member,
    )
    return ProviderCustodyGateway(policy, client=client), client  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_external_gateway_denies_frozen_and_collection_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, client = gateway(tmp_path, monkeypatch)
    principal = boundary.policy.principals["1" * 64]
    with pytest.raises(ProviderCustodyError, match="full-inventory freeze"):
        await boundary.proxy(
            principal,
            "DELETE",
            "/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicies/"
            "fs2-model-network-static-custody",
            "",
            b"",
            {},
        )
    with pytest.raises(ProviderCustodyError, match="collection-wide"):
        await boundary.proxy(
            principal,
            "DELETE",
            "/apis/apps/v1/namespaces/fs2-models/deployments",
            "labelSelector=app%3Dmodel",
            b"",
            {},
        )
    with pytest.raises(ProviderCustodyError, match="RBAC collection"):
        await boundary.proxy(
            principal,
            "PATCH",
            "/apis/rbac.authorization.k8s.io/v1/namespaces/fs2-models/roles/"
            "model-writer",
            "",
            b"{}",
            {"content-type": "application/merge-patch+json"},
        )
    assert client.requests == []


@pytest.mark.asyncio
async def test_operation_lease_requires_exact_principal_and_json_patch_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, client = gateway(tmp_path, monkeypatch)
    transition = boundary.policy.principals["1" * 64]
    wrong = boundary.policy.principals["2" * 64]
    path = (
        "/apis/coordination.k8s.io/v1/namespaces/fs2-system/leases/"
        "fs2-model-network-transition"
    )
    patch = [
        {"op": "test", "path": "/metadata/resourceVersion", "value": "17"},
        {"op": "test", "path": "/spec/holderIdentity", "value": ""},
        {
            "op": "replace",
            "path": "/spec/holderIdentity",
            "value": "testrun:123:0123456789abcdef0123456789abcdef",
        },
        {"op": "replace", "path": "/spec/leaseDurationSeconds", "value": 7200},
    ]
    body = json.dumps(patch).encode()
    headers = {"content-type": "application/json-patch+json"}
    with pytest.raises(ProviderCustodyError, match="method or principal"):
        await boundary.proxy(wrong, "PATCH", path, "", body, headers)
    without_cas = json.dumps(patch[1:]).encode()
    with pytest.raises(ProviderCustodyError, match="resourceVersion CAS"):
        await boundary.proxy(transition, "PATCH", path, "", without_cas, headers)
    without_holder_test = json.dumps([patch[0], *patch[2:]]).encode()
    with pytest.raises(ProviderCustodyError, match="one holder test"):
        await boundary.proxy(
            transition, "PATCH", path, "", without_holder_test, headers
        )
    overlong = json.loads(body)
    overlong[-1]["value"] = 7201
    with pytest.raises(ProviderCustodyError, match="duration exceeds"):
        await boundary.proxy(
            transition, "PATCH", path, "", json.dumps(overlong).encode(), headers
        )
    response = await boundary.proxy(transition, "PATCH", path, "", body, headers)
    assert response.status_code == 200
    assert len(client.requests) == 1
