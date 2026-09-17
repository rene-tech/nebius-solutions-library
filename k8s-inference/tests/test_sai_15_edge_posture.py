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
    assert "min_domains        = local.public_edge_enabled ? var.public_edge_availability_contract.minimum_domains : null" in terraform
    assert 'dynamic "affinity"' in terraform
    assert "required_during_scheduling_ignored_during_execution" in terraform
    assert "var.public_edge_availability_contract.node_selector" in terraform
    assert 'key      = "metadata.name"' in terraform
    assert "local.public_edge_membership_authority.serving_member_instance_ids" in terraform

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
        "terraform_data.public_edge_apply_eligibility[0]",
        "kubernetes_manifest.public_edge_node_authority_policy[0]",
        "kubernetes_manifest.public_edge_node_authority_binding[0]",
    )
    assert "31 + length(local.edge_rate_limit_managed_resource_addresses)" in outputs
    assert 'output "edge_rate_limit_managed_resource_addresses"' in outputs
    for address in addresses:
        assert f'"{address}"' in terraform

    foundation_locals = (ROOT / "stages/foundation/locals.tf").read_text(
        encoding="utf-8"
    )
    assert foundation_locals.count(
        "minDomains = var.public_edge_availability_contract.minimum_domains"
    ) == 2
    assert "minDomains        = local.public_edge_enabled ?" not in foundation_locals
    assert foundation_locals.count("tolerations = []") == 2
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
    assert values["envoyProxy"]["tolerations"] == []
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
    workload_locals = (ROOT / "stages/workloads/locals.tf").read_text(
        encoding="utf-8"
    )
    workload_control_plane = (ROOT / "stages/workloads/control_plane.tf").read_text(
        encoding="utf-8"
    )
    envoy_proxy_template = (
        ROOT
        / "charts/control-plane/fs2-serve-control-plane/templates/envoyproxy.yaml"
    ).read_text(encoding="utf-8")
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
    membership_trust_store = yaml.safe_load(
        (
            ROOT
            / "stages/foundation/trusted-public-edge-membership-issuers.json"
        ).read_text(encoding="utf-8")
    )
    provider_adapter_trust_store = yaml.safe_load(
        (
            ROOT
            / "stages/foundation/trusted-public-edge-provider-adapters.json"
        ).read_text(encoding="utf-8")
    )
    protected_launcher = (
        ROOT / "stages/foundation/scripts/public-edge-capsule-launcher.c"
    ).read_text(encoding="utf-8")
    node_authority = (
        ROOT / "stages/foundation/public_edge_node_authority.tf"
    ).read_text(encoding="utf-8")
    infrastructure_outputs = (ROOT / "stages/infrastructure/outputs.tf").read_text(
        encoding="utf-8"
    )
    infrastructure_variables = (
        ROOT / "stages/infrastructure/variables.tf"
    ).read_text(encoding="utf-8")
    infrastructure_cluster = (ROOT / "stages/infrastructure/cluster.tf").read_text(
        encoding="utf-8"
    )
    foundation_contract = (ROOT / "stages/foundation/cluster_contract.tf").read_text(
        encoding="utf-8"
    )
    foundation_locals = (ROOT / "stages/foundation/locals.tf").read_text(
        encoding="utf-8"
    )
    foundation_variables = (ROOT / "stages/foundation/variables.tf").read_text(
        encoding="utf-8"
    )
    workloads_variables = (ROOT / "stages/workloads/variables.tf").read_text(
        encoding="utf-8"
    )
    root_contract = (ROOT / "main.tf").read_text(encoding="utf-8")
    foundation_apply_gate = (
        ROOT / "stages/foundation/public_edge_apply_gate.tf"
    ).read_text(encoding="utf-8")
    workloads_apply_gate = (
        ROOT / "stages/workloads/public_edge_apply_gate.tf"
    ).read_text(encoding="utf-8")
    apply_gate_verifier = (
        ROOT
        / "stages/foundation/scripts/verify-public-edge-node-eligibility.py"
    ).read_text(encoding="utf-8")
    inference_stack = (ROOT / "inference-stack").read_text(encoding="utf-8")
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
    assert "edge identity verifier lacks the accepted capsule proof" in adapter
    assert '"edge-client-identity-verifier"' in adapter_contract
    assert "return len(chain)" in adapter
    assert trust_store == {
        "schema": "fs2-serve.nebius.ai/trusted-edge-evidence-issuers/v1",
        "issuers": [],
    }
    assert membership_trust_store == {
        "schema": "fs2-serve.nebius.ai/trusted-public-edge-membership-issuers/v3",
        "issuers": [],
    }
    assert provider_adapter_trust_store == {
        "schema": "fs2-serve.nebius.ai/trusted-public-edge-provider-adapters/v1",
        "adapters": [],
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
    assert 'output "public_edge_availability_contract_sha256"' in infrastructure_outputs
    assert 'schema               = "fs2-serve.nebius.ai/public-edge-availability/v3"' in infrastructure_variables
    assert 'topology_key    = "kubernetes.io/hostname"' in infrastructure_variables
    assert "minimum_domains = 3" in infrastructure_variables
    assert "minimum_available_nodes" in infrastructure_variables
    assert '"nebius.com/node-group-id"' in infrastructure_variables
    assert '"lifecycle.fs2.nebius/run"' in infrastructure_variables
    assert 'blocking_taint_effects = ["NoExecute", "NoSchedule"]' in infrastructure_variables
    assert 'data "terraform_remote_state" "infrastructure"' in foundation_contract
    assert "local.expected_infrastructure_state" in foundation_contract
    assert (
        "data.terraform_remote_state.infrastructure.outputs.public_edge_availability_contract_sha256"
        in foundation_contract
    )
    assert "data.terraform_remote_state.infrastructure.outputs.cluster_id" in foundation_contract
    assert "data.terraform_remote_state.infrastructure.outputs.target_contract" in foundation_contract
    assert 'data "kubernetes_resources" "public_edge_system_nodes"' in foundation_contract
    assert 'data "kubernetes_resources" "public_edge_system_nodes"' in workload_contract
    assert "public_edge_ready_node_preflight" in foundation_locals
    assert "distinct_hostname_count" in foundation_locals
    assert "resource_version" in foundation_locals
    assert "blocking_taints" in foundation_locals
    assert "public_edge_current_node_preflight" in workload_locals
    assert "resource_version" in workload_locals
    assert "blocking_taints" in workload_locals
    assert "eligible_nodes_sha256" in workload_contract
    assert "setsubtract(" in workload_contract
    assert (
        "nodeSelector = local.public_edge_enabled ? "
        "var.public_edge_availability_contract.node_selector"
        in workload_control_plane
    )
    assert "tolerations    = []" in workload_control_plane
    assert "nebius.com/node-group-id" in envoy_proxy_template
    assert "lifecycle.fs2.nebius/run" in envoy_proxy_template
    assert ".Values.envoyProxy.eligibleNodeNames" in envoy_proxy_template
    assert "matchFields:" in envoy_proxy_template
    assert "key: metadata.name" in envoy_proxy_template
    assert ".Values.envoyProxy.tolerations" in envoy_proxy_template
    for variables in (foundation_variables, workloads_variables):
        assert "update_strategy.max_unavailable == 0" in variables
        assert "update_strategy.max_surge >= 1" in variables
        assert "update_strategy.minimum_available_nodes >= 3" in variables
    assert 'var.public_edge_mode != "public" ||' in infrastructure_cluster
    assert "local.effective_system_pool.node_count >= 3" in infrastructure_cluster
    assert "local.effective_system_pool.max_unavailable == 0" in infrastructure_cluster
    assert "local.effective_system_pool.max_surge >= 1" in infrastructure_cluster
    assert 'var.deployment.edge.mode != "public" ||' in root_contract
    assert "local.effective_system_node_count >= 3" in root_contract
    assert "local.effective_system_max_unavailable == 0" in root_contract
    assert "local.effective_system_max_surge >= 1" in root_contract
    for apply_gate in (foundation_apply_gate, workloads_apply_gate):
        assert 'resource "terraform_data" "public_edge_apply_eligibility"' in apply_gate
        assert 'data "external" "public_edge_mutation_fence"' in apply_gate
        assert "plantimestamp()" in apply_gate
        assert "maximum_plan_age_seconds  = 14400" in apply_gate
        assert "FS2_EDGE_GATE_PLANNED_AT" in apply_gate
        assert "FS2_EDGE_GATE_NODE_GROUP_ID" in apply_gate
        assert "FS2_EDGE_GATE_NODE_SELECTOR_JSON" in apply_gate
        assert "verify-public-edge-node-eligibility.py" in apply_gate
        assert '"external"' in apply_gate
        assert "gate_id" in apply_gate
        assert 'public_edge_gate_launcher_path = "/usr/local/libexec/fs2-public-edge-gate-launcher"' in apply_gate
        assert "public_edge_gate_verifier_sha256" in apply_gate
        assert "interpreter = [local.public_edge_gate_launcher_path" in apply_gate
        assert "FS2_EDGE_GATE_POLICY_SHA256" in apply_gate
        assert "FS2_EDGE_GATE_BINDING_SHA256" in apply_gate
        assert "FS2_EDGE_GATE_PROVIDER_ADAPTER_TRUST_SHA256" in apply_gate
        assert "FS2_EDGE_GATE_NEBIUS_PROFILE" not in apply_gate
        assert '"/usr/bin/env"' not in apply_gate
    assert "launcher must be statically linked" in protected_launcher
    assert "clearenv()" in protected_launcher
    assert "expected a supported logical source and mode" in protected_launcher
    assert "launcher must be root:capsule mode 2755" in protected_launcher
    assert 'child[output++] = "-I"' in protected_launcher
    assert 'child[output++] = "-B"' in protected_launcher
    assert protected_launcher.index("clearenv()") < protected_launcher.index(
        "cannot restore explicit operator secret environment"
    )
    assert '"HOME": "/nonexistent"' in inference_stack
    assert inference_stack.index('if args.command == "apply":') < inference_stack.index(
        "require_terraform_version(args.terraform)"
    )
    assert "terraform_data.public_edge_apply_eligibility" in terraform
    assert "terraform_data.public_edge_apply_eligibility" in workload_control_plane
    assert "data.external.public_edge_mutation_fence" in terraform
    assert "data.external.public_edge_mutation_fence" in workload_control_plane
    assert 'result.verdict == "PASS"' in terraform
    assert 'result.verdict == "PASS"' in workload_control_plane
    assert '"terraform_data.public_edge_apply_eligibility[0]"' in terraform
    assert "local.public_edge_enabled ? 1 : 0" in (
        ROOT / "stages/workloads/outputs.tf"
    ).read_text(encoding="utf-8")
    assert '"cluster.x-k8s.io/owner-name"' in apply_gate_verifier
    assert '"cluster.x-k8s.io/cluster-name"' in apply_gate_verifier
    assert '"Node.spec.providerID"' in apply_gate_verifier
    assert 'r"nebius://computeinstance-[a-z0-9]+"' in apply_gate_verifier
    assert '"node-group",' in apply_gate_verifier
    assert 'compute_instance_cli = [*provider_observer, "compute", "instance"]' in apply_gate_verifier
    assert '"--all"' not in apply_gate_verifier
    assert '"--page-size", "100"' in apply_gate_verifier
    assert 'page_arguments.extend(("--page-token", page_token))' in apply_gate_verifier
    assert '"terminal_next_page_token": ""' in apply_gate_verifier
    assert '"list", "--parent-id", project_id' in apply_gate_verifier
    assert '"get", "--id", instance_id' in apply_gate_verifier
    assert "provider_member_ids=provider_ids" in apply_gate_verifier
    assert "Kubernetes Node provider IDs do not equal the provider member set" in apply_gate_verifier
    assert 'status_value.get("state") != "RUNNING"' in apply_gate_verifier
    assert 'status_value.get("reconciling") is not False' in apply_gate_verifier
    assert "before_revision != after_revision" in apply_gate_verifier
    assert apply_gate_verifier.count("plan_age = parse_plan_timestamp(") == 2
    assert "nodes_before" in apply_gate_verifier
    assert "nodes_after" in apply_gate_verifier
    assert "signed_instance_ids=signed_member_ids" in apply_gate_verifier
    assert "name_pattern" not in apply_gate_verifier
    assert "public-edge membership signature verification failed" in apply_gate_verifier
    assert "reopened provider membership export bytes" in apply_gate_verifier
    assert "executable identity differs from the signed toolchain" in apply_gate_verifier
    assert "executable changed between resolution and open" in apply_gate_verifier
    assert "running Python interpreter differs from the signed toolchain" in apply_gate_verifier
    assert "validate_parent_chain(path" in apply_gate_verifier
    assert 'f"/proc/self/fd/{pinned_tools[\'provider_observer\'][2]}"' in apply_gate_verifier
    assert 'f"/proc/self/fd/{pinned_tools[\'kubectl\'][2]}"' in apply_gate_verifier
    assert 'f"/proc/self/fd/{snapshot_descriptor}"' in apply_gate_verifier
    assert "pass_fds=PINNED_COMMAND_FDS" in apply_gate_verifier
    assert 'cwd="/"' in apply_gate_verifier
    assert '"PATH": CAPSULE_TOOL_BIN or "/usr/bin:/bin"' in apply_gate_verifier
    assert "signed kubectl differs from the accepted capsule executable" in apply_gate_verifier
    assert '"HOME": "/nonexistent"' in apply_gate_verifier
    assert "sealed_memfd(\"public-edge-kubeconfig\"" in apply_gate_verifier
    assert "FS2_CAPSULE_SOURCE_SHA256" in apply_gate_verifier
    assert '"ValidatingAdmissionPolicy after"' in apply_gate_verifier
    assert '"ValidatingAdmissionPolicyBinding after"' in apply_gate_verifier
    assert "terraform_json_sha256(after_contract)" in apply_gate_verifier
    assert apply_gate_verifier.index("nodes_after = run_json") < apply_gate_verifier.index(
        "group_list_after = paginated_list"
    )
    assert 'data "external" "public_edge_membership_contract"' in node_authority
    assert 'resource "kubernetes_manifest" "public_edge_node_authority_policy"' in node_authority
    assert 'resource "kubernetes_manifest" "public_edge_node_authority_binding"' in node_authority
    assert 'failurePolicy = "Fail"' in node_authority
    assert node_authority.count('matchPolicy = "Equivalent"') == 2
    assert 'operations  = ["CREATE", "UPDATE"]' in node_authority
    assert "object.spec.providerID == 'nebius://' + object.metadata.name" in node_authority
    assert "public_edge_protected_labels_unchanged_cel" in node_authority
    assert "public_edge_joining_labels_monotonic_cel" in node_authority
    assert "!(object.metadata.name in %s) && !(oldObject.metadata.name in %s)" in node_authority
    assert "request.userInfo" not in node_authority
    assert "membership-epoch-sequence" in node_authority
    assert "predecessor_payload_sha256" in node_authority
    assert "serving-member-instance-ids" in node_authority
    assert "joining-member-instance-ids" in node_authority
    assert "retiring-member-instance-ids" in node_authority
    assert "public_edge_existing_serving_member_instance_ids" in node_authority
    assert "public_edge_existing_joining_member_instance_ids" in node_authority
    assert "public_edge_existing_retiring_member_instance_ids" in node_authority
    assert "setsubtract(toset(local.public_edge_existing_serving_member_instance_ids)" in node_authority
    assert "local.public_edge_existing_phase == \"prepare\"" in node_authority
    assert "local.public_edge_existing_phase == \"cutover\"" in node_authority
    assert '"prepare"' in node_authority
    assert '"cutover"' in node_authority
    assert '"kubernetes_node_controller",' not in apply_gate_verifier
    assert "public_edge_node_authority_policy_sha256" in node_authority
    assert "public_edge_node_authority_binding_sha256" in node_authority
    for prerequisite in (
        "kubernetes_config_map_v1.edge_rate_limit_redis",
        "kubernetes_service_v1.edge_rate_limit_redis_headless",
        "kubernetes_service_v1.edge_rate_limit_redis_sentinel",
        "kubernetes_pod_disruption_budget_v1.edge_rate_limit_redis",
        "kubernetes_network_policy_v1.edge_rate_limit_redis",
    ):
        assert prerequisite in foundation_apply_gate
