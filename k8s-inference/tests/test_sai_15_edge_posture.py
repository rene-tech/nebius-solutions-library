from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_foundation_configures_shared_rate_limit_service_and_store() -> None:
    values = yaml.safe_load(
        (ROOT / "stages/foundation/values/envoy-gateway.yaml").read_text(encoding="utf-8")
    )

    assert values["deployment"]["replicas"] >= 2
    assert values["podDisruptionBudget"]["minAvailable"] == 1
    rate_limit = values["config"]["envoyGateway"]["rateLimit"]
    assert rate_limit["backend"] == {
        "type": "Redis",
        "redis": {
            "url": "fs2-edge-rate-limit-redis.envoy-gateway-system.svc.cluster.local:6379"
        },
    }
    rate_limit_deployment = values["config"]["envoyGateway"]["provider"]["kubernetes"][
        "rateLimitDeployment"
    ]
    assert rate_limit_deployment["replicas"] >= 2
    assert rate_limit_deployment["container"]["resources"]["requests"]["cpu"]
    assert rate_limit_deployment["container"]["resources"]["limits"]["cpu"]
    assert rate_limit_deployment["pod"]["topologySpreadConstraints"][0]["topologyKey"] == (
        "kubernetes.io/hostname"
    )

    terraform = (ROOT / "stages/foundation/edge_rate_limit.tf").read_text(encoding="utf-8")
    assert "docker.io/library/redis@sha256:" in terraform
    assert "automount_service_account_token = false" in terraform
    assert "read_only_root_filesystem  = true" in terraform
    assert 'resource "kubernetes_pod_disruption_budget_v1" "edge_rate_limit_redis"' in terraform
    assert 'resource "kubernetes_network_policy_v1" "edge_rate_limit_redis"' in terraform

    release = (ROOT / "stages/foundation/releases.tf").read_text(encoding="utf-8")
    assert 'values = [file("${path.module}/values/envoy-gateway.yaml")]' in release
    assert "kubernetes_service_v1.edge_rate_limit_redis" in release


def test_source_contract_does_not_regress_to_one_shared_local_bucket() -> None:
    policy = (
        ROOT
        / "charts/control-plane/fs2-serve-control-plane/templates/backendtrafficpolicy.yaml"
    ).read_text(encoding="utf-8")
    client_policy = (
        ROOT
        / "charts/control-plane/fs2-serve-control-plane/templates/clienttrafficpolicy.yaml"
    ).read_text(encoding="utf-8")
    proxy = (
        ROOT / "charts/control-plane/fs2-serve-control-plane/templates/envoyproxy.yaml"
    ).read_text(encoding="utf-8")

    assert "kind: Gateway" in policy
    assert "sectionName: {{ .Values.publicGateway.httpsListenerName }}" in policy
    assert "rateLimit:\n    global:" in policy
    assert "type: Distinct" in policy
    assert "value: /admin" in policy
    assert "rateLimit:\n    local:" not in policy
    assert "numTrustedHops: {{ .Values.edgeRateLimit.trustedHops }}" in client_policy
    assert "maxStreamDuration:" in client_policy
    assert "connectionLimit:" in client_policy
    assert "replicas: {{ .Values.envoyProxy.replicaCount }}" in proxy
    assert "envoyPDB:" in proxy
    assert "topologySpreadConstraints:" in proxy
