from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_foundation_configures_one_ha_rate_limit_authority_and_closed_count() -> None:
    values = yaml.safe_load(
        (ROOT / "stages/foundation/values/envoy-gateway.yaml").read_text(encoding="utf-8")
    )

    assert values["deployment"]["replicas"] >= 2
    assert values["podDisruptionBudget"]["minAvailable"] == 1
    rate_limit = values["config"]["envoyGateway"]["rateLimit"]
    assert rate_limit["backend"] == {
        "type": "Redis",
        "redis": {
            "url": (
                "fs2-edge-rate-limit,"
                "fs2-edge-rate-limit-redis-0.fs2-edge-rate-limit-redis-headless."
                "envoy-gateway-system.svc.cluster.local:26379,"
                "fs2-edge-rate-limit-redis-1.fs2-edge-rate-limit-redis-headless."
                "envoy-gateway-system.svc.cluster.local:26379,"
                "fs2-edge-rate-limit-redis-2.fs2-edge-rate-limit-redis-headless."
                "envoy-gateway-system.svc.cluster.local:26379"
            )
        },
    }
    assert rate_limit["failClosed"] is True
    rate_limit_deployment = values["config"]["envoyGateway"]["provider"]["kubernetes"][
        "rateLimitDeployment"
    ]
    assert rate_limit_deployment["replicas"] >= 2
    assert {item["name"]: item["value"] for item in rate_limit_deployment["container"]["env"]}[
        "REDIS_TYPE"
    ] == "sentinel"
    assert rate_limit_deployment["container"]["resources"]["requests"]["cpu"]
    assert rate_limit_deployment["container"]["resources"]["limits"]["cpu"]
    assert rate_limit_deployment["pod"]["topologySpreadConstraints"][0]["topologyKey"] == (
        "kubernetes.io/hostname"
    )

    terraform = (ROOT / "stages/foundation/edge_rate_limit.tf").read_text(encoding="utf-8")
    assert "docker.io/library/redis@sha256:" in terraform
    assert "automount_service_account_token = false" in terraform
    assert "read_only_root_filesystem  = true" in terraform
    assert 'resource "kubernetes_deployment_v1" "edge_rate_limit_redis"' not in terraform
    assert 'resource "kubernetes_stateful_set_v1" "edge_rate_limit_redis"' in terraform
    assert "replicas     = 3" in terraform
    assert "sentinel monitor $master_name $master_address $master_port 2" in terraform
    assert "SENTINEL get-master-addr-by-name" in terraform
    assert "replicaof %s %s" in terraform
    assert 'resource "kubernetes_service_v1" "edge_rate_limit_redis_headless"' in terraform
    assert 'resource "kubernetes_service_v1" "edge_rate_limit_redis_sentinel"' in terraform
    assert 'resource "kubernetes_pod_disruption_budget_v1" "edge_rate_limit_redis"' in terraform
    assert 'min_available = "2"' in terraform
    assert 'resource "kubernetes_network_policy_v1" "edge_rate_limit_redis"' in terraform
    assert "min_domains        = local.public_edge_enabled ? var.public_edge_availability_contract.minimum_domains : 1" in terraform
    assert 'dynamic "affinity"' in terraform
    assert "required_during_scheduling_ignored_during_execution" in terraform
    assert "var.public_edge_availability_contract.node_selector" in terraform

    release = (ROOT / "stages/foundation/releases.tf").read_text(encoding="utf-8")
    assert "yamlencode(local.envoy_gateway_edge_availability_values)" in release
    assert "kubernetes_stateful_set_v1.edge_rate_limit_redis" in release
    assert "kubernetes_service_v1.edge_rate_limit_redis_headless" in release
    assert "kubernetes_service_v1.edge_rate_limit_redis_sentinel" in release

    outputs = (ROOT / "stages/foundation/outputs.tf").read_text(encoding="utf-8")
    addresses = (
        "kubernetes_config_map_v1.edge_rate_limit_redis",
        "kubernetes_stateful_set_v1.edge_rate_limit_redis",
        "kubernetes_service_v1.edge_rate_limit_redis_headless",
        "kubernetes_service_v1.edge_rate_limit_redis_sentinel",
        "kubernetes_pod_disruption_budget_v1.edge_rate_limit_redis",
        "kubernetes_network_policy_v1.edge_rate_limit_redis",
    )
    assert "31 + length(local.edge_rate_limit_managed_resource_addresses)" in outputs
    assert 'output "edge_rate_limit_managed_resource_addresses"' in outputs
    for address in addresses:
        assert f'"{address}"' in terraform

    foundation_locals = (ROOT / "stages/foundation/locals.tf").read_text(
        encoding="utf-8"
    )
    assert foundation_locals.count("minDomains        = local.public_edge_enabled ?") == 2
    assert foundation_locals.count(
        "requiredDuringSchedulingIgnoredDuringExecution"
    ) == 2
    assert foundation_locals.count(
        "var.public_edge_availability_contract.node_selector"
    ) == 2


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
    assert "sectionName: {{ .Values.publicGateway.httpListenerName }}" in policy
    assert "rateLimit:\n    global:" in policy
    assert "type: Distinct" in policy
    assert "value: /admin" in policy
    assert "rateLimit:\n    local:" not in policy
    assert "verified edgeClientIdentity provider/LB contract" in policy
    assert "providerContractSha256" in policy
    assert "evidenceReceiptSha256" in policy
    assert "issuerKeyId" in policy
    assert "providerLoadBalancerId" in policy
    assert "directAccessExcluded" in policy
    assert "numTrustedHops: {{ .Values.edgeClientIdentity.trustedHops }}" in client_policy
    assert client_policy.count("clientIPDetection:") == 2
    assert "maxStreamDuration:" in client_policy
    assert "connectionLimit:" in client_policy
    assert "replicas: {{ .Values.envoyProxy.replicaCount }}" in proxy
    assert "envoyPDB:" in proxy
    assert "topologySpreadConstraints:" in proxy

    values = yaml.safe_load(
        (ROOT / "charts/control-plane/fs2-serve-control-plane/values.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert values["edgeClientIdentity"] == {
        "verified": False,
        "trustedHops": 0,
        "providerContractSha256": "",
        "evidenceReceiptSha256": "",
        "issuerKeyId": "",
        "providerLoadBalancerId": "",
        "directAccessExcluded": False,
    }
    assert values["edgeConnectionLimits"]["maxStreamDuration"] == "7500s"
    assert values["edgeConnectionLimits"]["maxConnectionDuration"] == "7800s"
    assert values["httpRoute"]["audioStreamRequestTimeout"] == "7500s"
    assert values["httpRoute"]["audioStreamBackendRequestTimeout"] == "7500s"
    assert values["envoyProxy"]["topologySpreadConstraints"][0]["minDomains"] == 3
    assert values["envoyProxy"]["affinity"]["podAntiAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ][0]["topologyKey"] == "kubernetes.io/hostname"

    route = (
        ROOT / "charts/control-plane/fs2-serve-control-plane/templates/httproute.yaml"
    ).read_text(encoding="utf-8")
    assert route.index("value: /v1/audio/stream") < route.index("value: /v1\n")
    assert "type: Exact\n            value: /v1/audio/stream" in route
    assert "request: {{ .Values.httpRoute.audioStreamRequestTimeout | quote }}" in route
    assert (
        "backendRequest: {{ .Values.httpRoute.audioStreamBackendRequestTimeout | quote }}"
        in route
    )

    root_variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
    workload_contract = (ROOT / "stages/workloads/cluster_contract.tf").read_text(
        encoding="utf-8"
    )
    adapter_contract = (ROOT / "stages/workloads/edge_client_identity.tf").read_text(
        encoding="utf-8"
    )
    adapter = (
        ROOT / "stages/workloads/scripts/verify-edge-client-identity-receipt.py"
    ).read_text(encoding="utf-8")
    trust_store = yaml.safe_load(
        (ROOT / "stages/workloads/contracts/trusted-edge-evidence-issuers.json").read_text(
            encoding="utf-8"
        )
    )
    infrastructure_outputs = (ROOT / "stages/infrastructure/outputs.tf").read_text(
        encoding="utf-8"
    )
    infrastructure_cluster = (ROOT / "stages/infrastructure/cluster.tf").read_text(
        encoding="utf-8"
    )
    root_contract = (ROOT / "main.tf").read_text(encoding="utf-8")
    assert "client_identity = optional(any)" in root_variables
    assert "var.deployment.edge.client_identity == null" in root_variables
    assert "var.deployment.edge.client_identity.verified" not in root_variables
    assert 'data "external" "edge_client_identity_receipt"' in adapter_contract
    assert "expected_subject_json" in adapter_contract
    assert "local.verified_edge_client_identity.verified" in workload_contract
    assert "local.verified_edge_client_identity.trusted_hops >= 1" in workload_contract
    assert "payload digest does not match the reopened payload" in adapter
    assert "reopened provider/LB evidence bytes" in adapter
    assert "source-trusted authority" in adapter
    assert "provider listeners do not bind the exact Gateway listeners" in adapter
    assert "signed SG/routing facts do not exclude direct Envoy access" in adapter
    assert "return len(chain)" in adapter
    assert trust_store == {
        "schema": "fs2-serve.nebius.ai/trusted-edge-evidence-issuers/v1",
        "issuers": [],
    }
    for identity in (
        "cluster_id",
        "network_id",
        "subnet_id",
        "worker_security_group_id",
        "public_edge_ingress_rule_id",
        "security_group_source_cidrs",
    ):
        assert identity in infrastructure_outputs
    assert 'output "public_edge_availability_contract"' in infrastructure_outputs
    assert 'schema               = "fs2-serve.nebius.ai/public-edge-availability/v1"' in infrastructure_outputs
    assert 'topology_key   = "kubernetes.io/hostname"' in infrastructure_outputs
    assert "minimum_domains = 3" in infrastructure_outputs
    assert 'var.public_edge_mode != "public" ||' in infrastructure_cluster
    assert "local.effective_system_pool.node_count >= 3" in infrastructure_cluster
    assert 'var.deployment.edge.mode != "public" ||' in root_contract
    assert "local.effective_system_node_count >= 3" in root_contract
