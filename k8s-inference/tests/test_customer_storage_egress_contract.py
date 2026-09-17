"""Fail-closed tests for the signed Nebius customer-storage egress contract."""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPT = (
    Path(__file__).parents[1]
    / "stages/workloads/scripts/customer_storage_egress_contract.py"
)
TERRAFORM = Path(__file__).parents[1] / "stages/workloads/customer_storage.tf"
SECURITY_ROOT = Path(__file__).parents[1] / "security/customer-storage-egress-boundary"
PROVIDER_AUTHORITY_ROOT = (
    Path(__file__).parents[1] / "security/customer-storage-egress-authority"
)
V2_CHART = Path(__file__).parents[1] / "charts/security/customer-storage-reconciler-v2"
SPEC = importlib.util.spec_from_file_location(
    "customer_storage_egress_contract", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
contract_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract_module)

NOW = datetime(2026, 9, 16, 18, tzinfo=UTC)


def resolver(host: str, port: int, *, type: int):
    assert port == 443 and type == socket.SOCK_STREAM
    addresses = {
        "cpl.iam.api.nebius.cloud": "198.51.100.10",
        "cpl.storage.api.nebius.cloud": "198.51.100.11",
        "tokens.iam.api.nebius.cloud": "2001:db8::12",
    }
    address = addresses[host]
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]


def resign(value: dict, private_key: Ed25519PrivateKey) -> dict:
    body = {
        key: item
        for key, item in value.items()
        if key not in {"payload_sha256", "signature"}
    }
    payload = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    value["payload_sha256"] = hashlib.sha256(payload).hexdigest()
    value["signature"] = base64.b64encode(private_key.sign(payload)).decode()
    return value


@pytest.fixture
def signed_contract():
    private_key = Ed25519PrivateKey.generate()
    contract = contract_module.create_contract(private_key, now=NOW, resolver=resolver)
    return private_key, contract


def test_signed_contract_requires_live_provider_equality_and_host_routes(
    signed_contract,
):
    private_key, contract = signed_contract
    result = contract_module.verify_contract(
        contract, private_key.public_key(), now=NOW, resolver=resolver
    )
    assert json.loads(result["cidrs_json"]) == [
        "198.51.100.10/32",
        "198.51.100.11/32",
        "2001:db8::12/128",
    ]
    assert len(result["contract_sha256"]) == 64
    assert contract["sdk_services"] == contract_module.expected_sdk_services()
    assert {item["endpoint"] for item in contract["sdk_services"]} == set(
        contract["endpoints"]
    )


def test_endpoint_authority_matches_pinned_generated_sdk():
    assert (
        contract_module.provider_sdk_services()
        == contract_module.expected_sdk_services()
    )


def test_workloads_root_only_reads_the_external_versioned_boundary() -> None:
    source = TERRAFORM.read_text(encoding="utf-8")
    assert 'data "kubernetes_config_map_v1" "customer_storage_egress_trust"' in source
    assert (
        'data "kubernetes_config_map_v1" "customer_storage_egress_contract"' in source
    )
    assert (
        'data "kubernetes_resource" "customer_storage_egress_network_policy"' in source
    )
    assert (
        'data "kubernetes_resource" "customer_storage_egress_boundary_policy"' in source
    )
    assert (
        'data "kubernetes_resource" "customer_storage_egress_boundary_binding"'
        in source
    )
    assert (
        'resource "kubernetes_config_map_v1" "customer_storage_egress_contract"'
        not in source
    )
    assert (
        'resource "kubernetes_manifest" "customer_storage_egress_admission_policy"'
        not in source
    )
    assert (
        'resource "kubernetes_manifest" "customer_storage_egress_admission_binding"'
        not in source
    )
    assert "self.immutable == true" in source
    assert (
        'data.kubernetes_config_map_v1.customer_storage_egress_trust[0].data["public-key.pem"]'
        in source
    )
    assert "fs2-serve.nebius.ai/customer-storage-egress-security-handoff/v12" in source
    assert '"uv"' in source and '"--frozen"' in source
    assert "egress_contract_public_key_pem" not in source


def test_separate_security_owner_is_append_only_and_credential_isolated() -> None:
    source = (SECURITY_ROOT / "main.tf").read_text(encoding="utf-8")
    providers = (SECURITY_ROOT / "providers.tf").read_text(encoding="utf-8")
    variables = (SECURITY_ROOT / "variables.tf").read_text(encoding="utf-8")
    identity = (SECURITY_ROOT / "verify_owner_identity.py").read_text(
        encoding="utf-8"
    )
    control_plane = (TERRAFORM.parent / "control_plane.tf").read_text(encoding="utf-8")

    assert "var.security_owner_kubeconfig_path" in providers
    assert '"uv"' in source and '"--frozen"' in source
    assert "workloads_kubeconfig_path" in variables
    assert "_credential_sha256(path)" in identity
    assert 'resource "kubernetes_manifest" "boundary_policy"' in source
    assert 'resource "kubernetes_manifest" "boundary_binding"' in source
    assert 'resource "kubernetes_config_map_v1" "contract"' in source
    assert 'resource "kubernetes_config_map_v1" "trust"' in source
    assert 'resource "kubernetes_network_policy_v1" "contract"' in source
    assert 'failurePolicy = "Fail"' in source
    assert 'operations  = ["CREATE", "UPDATE", "DELETE"]' in source
    assert "security generations are create-only" in source
    assert "request.userInfo.groups.exists" in source
    assert "security-owner-subject-sha256" in source
    assert "workloads-subject-sha256" in source
    assert "fs2:customer-storage-egress-security-owner" in variables
    assert "non_owner_identities" in variables
    for probe in (
        "impersonate-users",
        "impersonate-groups",
        "impersonate-serviceaccounts",
        "impersonate-uids",
        "impersonate-userextras",
        "serviceaccount-tokenrequest",
        "bind-clusterroles",
        "escalate-clusterroles",
        "create-clusterrolebindings",
        "secrets",
        "pods/exec",
        "pods/attach",
        "pods/ephemeralcontainers",
        "certificatesigningrequests",
        "approve-certificate-signing-requests",
        "serviceaccounts",
        "deployments.apps",
    ):
        assert probe in identity
    assert "security-owner credential can update or delete" in identity
    assert source.count("prevent_destroy = true") >= 6
    assert "for_each = local.legacy_contracts" in source
    assert "for_each = local.successor_contracts" in source
    assert "for_each = local.legacy_trusts" in source
    assert "for_each = local.successor_trusts" in source
    assert (
        "for_each = var.provider_authority.retained_legacy_boundary_policies"
        in source
    )
    assert "customer_storage_external_egress_boundary" in control_plane


def test_provider_authority_is_external_content_bound_and_non_destructive() -> None:
    source = (PROVIDER_AUTHORITY_ROOT / "main.tf").read_text(encoding="utf-8")
    verifier = (PROVIDER_AUTHORITY_ROOT / "verify_authority_ledger.py").read_text(
        encoding="utf-8"
    )
    identity_verifier = (
        PROVIDER_AUTHORITY_ROOT / "verify_provider_identity.py"
    ).read_text(encoding="utf-8")
    readme = (PROVIDER_AUTHORITY_ROOT / "README.md").read_text(encoding="utf-8")
    retention = (TERRAFORM.parent / "customer_storage_retention.tf").read_text(
        encoding="utf-8"
    )

    assert 'resource "nebius_vpc_v1_security_group" "generation"' in source
    assert 'resource "nebius_mk8s_v1_node_group" "generation"' in source
    assert 'resource "terraform_data" "external_authority_v4"' in source
    assert 'destination_ports = [443]' in source
    assert 'destination_ports = [53]' in source
    assert 'destination_ports = [5432]' in source
    assert 'destination_cidrs = ["0.0.0.0/0"]' not in source
    assert source.count("prevent_destroy = true") >= 8
    assert source.count("for_each = local.all_generations") == 6
    assert source.count("ignore_changes  = all") >= 7
    assert "/etc/fs2-security-ro/authority/customer-storage-egress-authority.json" in verifier
    assert "/var/lib/fs2-security-checkpoints-ro/customer-storage-egress-prior-head.json" in verifier
    assert "O_NOFOLLOW" in verifier
    assert "before.st_uid != 0" in verifier
    assert "ST_RDONLY" in verifier
    assert "approved_manifest_sha256" in verifier
    assert "parent_manifest_sha256" in verifier
    assert "live_custody_sha256" in verifier
    assert "workloads_state_lineage" in verifier
    assert 'NEBIUS_TERRAFORM_PROVIDER_VERSION = "0.5.232"' in verifier
    assert "kubernetes_rbac_inventory_receipt" in verifier
    assert "cluster_access_principal_ids" in verifier
    assert "provider_effective_authority_graph_receipt" in verifier
    assert "graph_mutating_ids != [registry[\"authority_group_id\"]]" in verifier
    assert "head_generation_sha256" in verifier
    assert "provider_state_custody" in verifier
    assert '"generation_chain"' in verifier
    assert '"generation_chain_anchor_sha256"' in verifier
    assert 'installed_predecessor != prior_head["head_generation_sha256"]' in verifier
    assert '"authority_gate_generations"' in verifier
    assert '"retained_generations"' in verifier
    assert "boundary_policy_sha256" in verifier
    assert "nebius_terraform_provider_version" in verifier
    assert "predecessor_sha256" in verifier
    assert "predecessor_compatibility_sha256" in verifier
    assert "generation[-12:] != content_digest[:12]" in verifier
    assert '"iam", "whoami"' in identity_verifier
    assert '"group-membership"' in identity_verifier
    assert '"access-permit"' in identity_verifier
    assert '"auth-public-key"' in identity_verifier
    assert 'item["role"] == "admin"' in identity_verifier
    assert "MAX_KEY_LIFETIME = timedelta(days=90)" in identity_verifier
    assert "state" in readme and "omitting" in readme
    assert "removed {" not in retention
    assert retention.count("prevent_destroy = true") == 3
    assert retention.count("ignore_changes  = all") == 3
    assert 'resource "kubernetes_config_map_v1" "customer_storage_egress_contract"' in retention
    assert 'resource "kubernetes_manifest" "customer_storage_egress_admission_policy"' in retention
    assert "destroy = true" not in retention
    plan_gate = (Path(__file__).parents[1] / "security/verify_additive_plan.py").read_text(
        encoding="utf-8"
    )
    assert "SAFE_ACTIONS" in plan_gate
    assert '("forget",)' not in plan_gate
    assert "non-additive Terraform action is forbidden" in plan_gate


def test_integration_dependencies_and_remote_state_fail_closed() -> None:
    verifier = (
        Path(__file__).parents[1] / "security/verify_sai08_integration_dependencies.py"
    ).read_text(encoding="utf-8")
    dependencies = json.loads(
        (Path(__file__).parents[1] / "security/sai-08-integration-dependencies.json").read_text(
            encoding="utf-8"
        )
    )
    authority_versions = (
        Path(__file__).parents[1]
        / "security/customer-storage-egress-authority/versions.tf"
    ).read_text(encoding="utf-8")
    boundary_versions = (
        Path(__file__).parents[1]
        / "security/customer-storage-egress-boundary/versions.tf"
    ).read_text(encoding="utf-8")

    assert dependencies["status"] == "blocked"
    assert all(value is None for value in dependencies["dependencies"].values())
    assert 'os.environ.get("HELM_DRIVER") != "configmap"' in verifier
    assert '"show", "-s", "--format=%T"' in verifier
    assert '"merge-base", "--is-ancestor"' in verifier
    assert "kubernetes_rbac_inventory_receipt_sha256" in verifier
    assert 'backend "s3" {}' in authority_versions
    assert 'version = "= 0.5.232"' in authority_versions
    assert 'backend "s3" {}' in boundary_versions


def test_v2_chart_is_additive_selector_safe_and_effective_policy_gated() -> None:
    template = (V2_CHART / "templates/reconciler.yaml").read_text(encoding="utf-8")
    values = (V2_CHART / "values.yaml").read_text(encoding="utf-8")
    readme = (V2_CHART / "README.md").read_text(encoding="utf-8")
    legacy_helpers = (
        Path(__file__).parents[1]
        / "charts/control-plane/fs2-serve-control-plane/templates/_helpers.tpl"
    ).read_text(encoding="utf-8")

    assert "app.kubernetes.io/component: storage-reconciler-v3" in template
    assert "--kubernetes-network-policy-set" in template
    assert 'verbs: ["get", "list"]' in template
    assert "helm.sh/resource-policy: keep" in template
    assert "nodeSelector:" in template and "tolerations:" in template
    assert "acceptedSai10Commit" in values
    assert "acceptedSai10Tree" in values
    assert "priorHeadReceiptSha256" in values
    assert "predecessor-deployment-uid" in template
    assert "predecessor-receipt-sha256" in template
    assert "predecessorCompatibilitySha256" in values
    assert "rollout:" in values
    assert "rollout generation must be content-bound" in template
    assert "each additive rollout requires its exact generation-named Helm release" in template
    assert 'resource "helm_release" "storage_reconciler_v2"' in (
        SECURITY_ROOT / "main.tf"
    ).read_text(encoding="utf-8")
    assert "kind: Role\n" not in template
    assert "kind: RoleBinding\n" not in template
    boundary = (SECURITY_ROOT / "main.tf").read_text(encoding="utf-8")
    assert 'resource "kubernetes_role_v1" "reconciler_inventory"' in boundary
    assert 'resource "kubernetes_role_binding_v1" "reconciler_inventory"' in boundary
    assert "boundary_policy_sha256 = sha256(jsonencode(local.boundary_policy_spec))" in boundary
    assert "workload_policy_sha256 = sha256(jsonencode(local.workload_policy_spec))" in boundary
    assert "type: Recreate" not in template
    assert "fixed predecessor" in readme
    assert "storage-egress-generation" not in legacy_helpers.split(
        '{{- define "fs2-serve.storageSelectorLabels" -}}', 1
    )[1].split("{{- end -}}", 1)[0]


def test_security_owner_contract_rotation_is_versioned_and_overlapping() -> None:
    source = (SECURITY_ROOT / "main.tf").read_text(encoding="utf-8")
    readme = (SECURITY_ROOT / "README.md").read_text(encoding="utf-8")

    for prefix in (
        "fs2-customer-storage-egress-contract-${generation}",
        "fs2-customer-storage-egress-trust-${generation}",
        "fs2-customer-storage-egress-${generation}",
        "fs2-storage-v3-contract-${generation}",
        "fs2-storage-v3-trust-${generation}",
        "fs2-storage-v3-network-policy-${generation}",
        "fs2-storage-v3-boundary-${generation}",
    ):
        assert prefix in source
    assert '"fs2.nebius.ai/storage-egress-generation" = each.key' in source
    assert (
        '"fs2.nebius.ai/storage-egress-generation" = var.customer_storage.egress_boundary.generation'
        in TERRAFORM.read_text(encoding="utf-8")
    )
    assert "append-only" in readme


def test_sai08_external_authority_workload_and_state_closure_regression() -> None:
    authority = (PROVIDER_AUTHORITY_ROOT / "verify_authority_ledger.py").read_text(
        encoding="utf-8"
    )
    provider_identity = (
        PROVIDER_AUTHORITY_ROOT / "verify_provider_identity.py"
    ).read_text(encoding="utf-8")
    provider_backend = (
        PROVIDER_AUTHORITY_ROOT / "verify_backend_custody.py"
    ).read_text(encoding="utf-8")
    provider = (PROVIDER_AUTHORITY_ROOT / "main.tf").read_text(encoding="utf-8")
    boundary = (SECURITY_ROOT / "main.tf").read_text(encoding="utf-8")
    protected_lane = (SECURITY_ROOT / "protected_lane_admission.py").read_text(
        encoding="utf-8"
    )
    boundary_variables = (SECURITY_ROOT / "variables.tf").read_text(
        encoding="utf-8"
    )
    owner = (SECURITY_ROOT / "verify_owner_identity.py").read_text(
        encoding="utf-8"
    )
    boundary_backend = (SECURITY_ROOT / "verify_backend_custody.py").read_text(
        encoding="utf-8"
    )
    dependency = (
        Path(__file__).parents[1] / "security/verify_sai08_integration_dependencies.py"
    ).read_text(encoding="utf-8")
    control_plane = (TERRAFORM.parent / "control_plane.tf").read_text(
        encoding="utf-8"
    )
    rbac_capture = (
        SECURITY_ROOT / "capture_kubernetes_rbac_inventory.py"
    ).read_text(encoding="utf-8")
    node_attestation_capture = (
        SECURITY_ROOT / "capture_protected_lane_attestation.py"
    ).read_text(encoding="utf-8")
    lane_provisioning = (
        Path(__file__).parents[1]
        / "security/customer-storage-lane-provisioning/main.tf"
    ).read_text(encoding="utf-8")
    lane_receipt_capture = (
        Path(__file__).parents[1]
        / "security/customer-storage-lane-provisioning/capture_provisioning_receipt.py"
    ).read_text(encoding="utf-8")
    controller_audit = (
        PROVIDER_AUTHORITY_ROOT / "verify_controller_audit.py"
    ).read_text(encoding="utf-8")
    daemonset_live_verifier = (
        SECURITY_ROOT / "verify_live_daemonset_inventory.py"
    ).read_text(encoding="utf-8")

    assert 'ACCEPTED_SAI10 = "057386a3e0c616d79735adb43a97c19c48046608"' in dependency
    assert 'if not _is_ancestor(ACCEPTED_SAI10, "HEAD")' in dependency
    assert "provider-effective-authority-graph/v1" in authority
    assert "origin_resource_ids" in authority
    assert "graph_cluster_access_ids" in authority
    assert "graph_mutating_ids" in authority
    assert "effective_principal_ids" in provider_identity
    assert "AUTHORITY_GRAPH_ADAPTER" in provider_identity
    assert "provider authority adapter custody differs" in provider_identity
    assert "provider_principal_ids" not in (
        PROVIDER_AUTHORITY_ROOT / "capture_provider_iam_inventory.py"
    ).read_text(encoding="utf-8")
    assert "head_generation_sha256" in authority
    assert 'predecessor = prior_head["head_generation_sha256"]' in authority
    for verifier in (provider_backend, boundary_backend):
        assert "O_NOFOLLOW" in verifier
        assert 'backend.get("type") != "s3"' in verifier
        assert 'config.get("use_lockfile") is not True' in verifier
        assert '"state", "pull"' in verifier
        assert '"state", "list"' in verifier
        assert "state_version_adapter_sha256" in verifier

    for resource in (
        '"pods"',
        '"serviceaccounts"',
        '"deployments"',
        '"daemonsets"',
        '"statefulsets"',
        '"replicasets"',
        '"jobs"',
        '"cronjobs"',
    ):
        assert resource in boundary
    assert "secretKeyRef" in boundary
    assert "allowed_secret_names_cel" in boundary
    assert "provider_authority.node_selector_value" in boundary
    assert "(!has(POD.nodeName) || POD.nodeName == '')" in boundary
    assert "controller_identity_cel" in boundary
    assert "controller_identities_json" in boundary
    assert "pod-template-hash=FS2_POD_TEMPLATE_HASH" in boundary
    assert 'matchPolicy   = "Equivalent"' in boundary
    assert "object.spec == ${jsonencode(local.current_network_policy_spec)}" in boundary
    assert 'resource "kubernetes_manifest" "workload_policy_v3"' in boundary
    assert 'resource "kubernetes_manifest" "workload_binding_v3"' in boundary
    assert 'resource "kubernetes_manifest" "boundary_policy_v3"' in boundary
    assert 'resource "terraform_data" "security_generation_v4"' in boundary
    assert "successor_workload_policy_generations" in boundary
    assert "protected_node_target_cel" in boundary
    assert 'data "external" "protected_lane_admission"' in boundary
    assert "protected_lane_admission.py" in boundary
    assert "old_pod_target_cel" in boundary
    assert "old_template_target_cel" in boundary
    assert "observer_daemonset_allow_cel" in boundary
    assert "observer_pod_allow_cel" in boundary
    assert "OBSERVER_CLASSES" in protected_lane
    assert "critical-blanket-agent" in protected_lane
    assert "(!has(toleration.effect) || toleration.effect == ''" in protected_lane
    assert "has({path}.nodeName)" in protected_lane
    assert "nodeName == {node_name}" in protected_lane
    assert "protected_node_inventory_sha256" in protected_lane
    assert "protected_node_inventory_sha256" in authority
    assert "protected_node_scheduling_labels_sha256" in protected_lane
    assert "protected_node_scheduling_labels_sha256" in authority
    assert "protected_node_attestations" in protected_lane
    assert "_request_identity_matches" in protected_lane
    assert "_tolerates_protected_taint" in protected_lane
    assert "Any exact-key" in protected_lane
    assert 'resource "nebius_mk8s_v1_node_group" "stable_lane"' in provider
    assert "stable_provider_generations" in provider
    assert "provisioning-generation" in provider
    assert "min_node_count = try(each.value.min_node_count, 0)" in provider
    assert "max_node_count = try(each.value.max_node_count, 1)" in provider
    assert "fixed_node_count = null" in provider
    assert 'entry.get("min_node_count") != 1' in authority
    assert 'entry.get("max_node_count") != 1' in authority
    assert "kube_system_daemon_pod_cel" not in boundary
    assert "protected_node_pod_spec_cel" not in boundary
    assert "inventory_role_name_cel" in boundary
    assert "inventory_role_content_cel" in boundary
    assert "inventory_binding_content_cel" in boundary
    assert "nondelegatable generation-named NetworkPolicy inventory Role" in boundary
    assert "object.rules[0].resources == ['networkpolicies']" in boundary
    assert "object.rules[0].verbs == ['get','list']" in boundary
    assert "object.subjects[0].name == object.metadata.name" in boundary
    assert "object.roleRef.name == object.metadata.name" in boundary
    assert (
        "request.userInfo.groups.exists(group, group == '${var.security_owner_group}')"
        in boundary
    )
    assert 'resources   = ["pods/binding"]' in boundary
    assert "controller_identities" in boundary
    assert "audit-proven live kube-system controller ServiceAccounts" in boundary_variables
    assert "system:controller:daemon-set-controller" not in boundary_variables
    assert '"--all-namespaces"' in rbac_capture
    assert '"blanket_tolerating_agents"' in rbac_capture
    assert '"controller_identities"' in rbac_capture
    assert '"controller_audit_receipt_sha256"' in rbac_capture
    assert '"daemonset_list_resource_version"' in rbac_capture
    assert '"--resource-version-match=Exact"' in rbac_capture
    assert "controller-identities-json" not in rbac_capture
    assert "verify_live_controller_audit" in rbac_capture
    assert "AUDIT_ADAPTER_PATH" in controller_audit
    assert "live authenticated audit evidence differs" in controller_audit
    assert '"resourceVersion"' in node_attestation_capture
    assert 'node_spec.get("providerID")' in node_attestation_capture
    assert 'member.get("provider_id") == provider_id' in node_attestation_capture
    assert 'resource "nebius_mk8s_v1_node_group" "lane"' in lane_provisioning
    assert "provisioning_manifest_json" in lane_provisioning
    assert 'generation.get("min_node_count") != 1' in (
        Path(__file__).parents[1]
        / "security/customer-storage-lane-provisioning/verify_provisioning_manifest.py"
    ).read_text(encoding="utf-8")
    assert "CUSTODY_ADAPTER" in lane_receipt_capture
    assert '"backend_custody"' in lane_receipt_capture
    assert '"provider_inventory"' in lane_receipt_capture
    assert '"members"' in lane_receipt_capture
    assert "verify_live_lane_custody" in authority
    assert "LANE_CUSTODY_ADAPTER" in authority
    assert "_daemonset_snapshot" in daemonset_live_verifier
    assert 'data "external" "live_daemonsets_post_guard"' in boundary
    assert 'resource "terraform_data" "daemonset_inventory_post_guard"' in boundary
    assert "controller_identity_cel.node_health" in boundary
    assert "object.spec.providerID" in boundary
    assert "stable_provider_generations = {}" in provider
    assert 'data "kubernetes_resource" "protected_node_post_guard"' in boundary
    assert "protected_node_exempt_namespaces_cel" not in boundary
    assert "request.subResource == 'binding'" in boundary
    assert "request.subResource == ''" in boundary
    assert "current_release_helm_record_name" in boundary
    assert 'data "kubernetes_resource" "retained_boundary_policy_v3"' in boundary
    assert 'data "kubernetes_resource" "retained_workload_policy_v3"' in boundary
    assert "rbac_subjects" in owner
    assert "undeclared ServiceAccount subject" in owner
    assert '"pods/binding"' in owner
    assert '"nodes/proxy"' in owner
    assert '"impersonate-users": ("impersonate", "users")' in owner
    assert "effective_authority_sha256" in owner
    assert "verify_subject_inventory" in owner
    assert "reject_unapproved_dangerous" in owner
    assert "subject_authority" in owner
    assert "retained_legacy_boundary_policies" in boundary
    assert "retained_legacy_workload_policies" in boundary
    assert "accepted_non_owner_usernames_cel" in boundary
    assert 'data "kubernetes_resource" "retained_legacy_boundary_policy"' in boundary
    assert 'data "kubernetes_resource" "retained_legacy_workload_policy"' in boundary
    assert '"state", "pull"' in provider_backend
    assert '"state", "pull"' in boundary_backend
    workloads = TERRAFORM.read_text(encoding="utf-8")
    assert "customer_storage_reconciler_pods" in workloads
    assert "pod_label_sets_json" in workloads
    assert "boundary_policy_sha256" in workloads
    assert 'resource "helm_release" "control_plane"' in control_plane
    assert "prevent_destroy = true" in control_plane
    assert "atomic           = false" in control_plane
    assert "cleanup_on_fail  = false" in control_plane
    apply_wrapper = (Path(__file__).parents[1] / "security/apply_custodied_additive_plan.py").read_text(encoding="utf-8")
    assert 'SAFE_ACTIONS = {("no-op",), ("read",), ("create",)}' in apply_wrapper
    assert '"apply", "-input=false"' in apply_wrapper
    assert "source_commit" in apply_wrapper and "successor_state_contract" in apply_wrapper
    assert "version_before != version_after" in apply_wrapper
    assert "EXECUTION_PUBLIC_KEY" in apply_wrapper
    assert 'parser.add_argument("--public-key"' not in apply_wrapper
    assert '"TF_DATA_DIR"' in apply_wrapper
    assert "Terraform backend descriptor" in apply_wrapper
    assert "_verify_sai10_ancestry" in apply_wrapper
    assert "customer_storage_integration_dependencies" in workloads


def test_v12_fence_singleton_and_semantic_provider_custody_are_source_bound() -> None:
    authority = (PROVIDER_AUTHORITY_ROOT / "verify_authority_ledger.py").read_text(
        encoding="utf-8"
    )
    fence = (
        PROVIDER_AUTHORITY_ROOT / "verify_daemonset_admission_fence.py"
    ).read_text(encoding="utf-8")
    boundary = (SECURITY_ROOT / "main.tf").read_text(encoding="utf-8")
    provisioning = (
        Path(__file__).parents[1]
        / "security/customer-storage-lane-provisioning/main.tf"
    ).read_text(encoding="utf-8")
    capture = (
        Path(__file__).parents[1]
        / "security/customer-storage-lane-provisioning/capture_provisioning_receipt.py"
    ).read_text(encoding="utf-8")

    assert "POLICY_SPEC" in fence
    assert "_validate_binding" in fence
    assert "_validate_snapshot_ledger" in fence
    assert '"policy_spec": receipt["policy_spec"]' in fence
    assert '"binding_spec": receipt["binding_spec"]' in fence
    assert '"snapshot_ledger": receipt["snapshot_ledger"]' in fence
    assert "live != expected_live" in fence
    assert 'receipt.get("continuous_enforcement") is not True' in fence
    assert '"snapshot_ledger_head_sha256"' in fence
    assert 'resource "terraform_data" "daemonset_admission_fence"' in boundary
    assert "terraform_data.daemonset_admission_fence" in boundary
    assert "Both reads occur before binding" in boundary
    assert "terraform_data.protected_node_post_guard_attestation" in boundary
    assert "retained pre-fence protected-lane policy cannot be" in authority
    assert "GENERATIONAL_SINGLETON_RETAIN_PREDECESSOR" in authority
    assert "expected_lane_managed_addresses" in authority
    assert "expected_lane_rules" in authority
    assert 'operations  = ["CREATE", "UPDATE", "DELETE"]' in boundary
    assert "cannot be created, replaced or deleted" in boundary
    assert "max_surge       = { count = 0 }" in provisioning
    assert "max_unavailable = { count = 0 }" in provisioning
    assert 'backend.get("managed_addresses") != expected_managed_addresses' in capture
    assert "normalized_rules != expected_rules" in capture
    assert "len(members) != 1" in capture
    assert 'node_group.get("security_group_ids") != [security_group.get("id")]' in capture
    attestation = (
        SECURITY_ROOT / "capture_protected_lane_attestation.py"
    ).read_text(encoding="utf-8")
    assert "protected-lane-provisioning-receipt/v3" in attestation
    assert 'receipt.get("node_lifecycle_mode")' in attestation
    assert "len(members) != 1" in attestation


def test_predecessor_vap_compatibility_is_signed_and_selector_disjoint() -> None:
    authority = (PROVIDER_AUTHORITY_ROOT / "verify_authority_ledger.py").read_text(
        encoding="utf-8"
    )
    boundary = (SECURITY_ROOT / "main.tf").read_text(encoding="utf-8")
    output = (SECURITY_ROOT / "outputs.tf").read_text(encoding="utf-8")
    capture = (SECURITY_ROOT / "capture_predecessor_receipt.py").read_text(
        encoding="utf-8"
    )

    assert 'predecessor_label = "storage-reconciler"' in boundary
    assert 'successor_label   = "storage-reconciler-v2"' in boundary
    assert "predecessor_compatibility_sha256 = sha256(jsonencode(" in boundary
    assert "var.provider_authority.predecessor_compatibility_sha256" in boundary
    assert "predecessor_compatibility_sha256" in authority
    assert "receipt_sha256" in output
    assert "v3_selector_matches_old_object_cel" in boundary
    assert "term.operator == 'NotIn'" in boundary
    assert "request.operation == 'CREATE'" in boundary
    assert "!has(oldObject.spec.podSelector.matchLabels)" not in boundary
    assert "from verify_owner_identity import _kubectl" in capture
    assert "storage-reconciler-v2" in capture
    assert '"receipt_sha256": _digest(receipt)' in capture
    assert '"secret"' not in capture.lower()
    assert "Old resources remain" in readme
    assert "-replace" in readme


@pytest.mark.parametrize(
    "cidrs",
    [
        ["0.0.0.0/1", "128.0.0.0/1"],
        ["2000::/3"],
        ["203.0.113.44/32"],
        [],
    ],
)
def test_rejects_aggregate_broad_arbitrary_and_empty_sets(signed_contract, cidrs):
    private_key, contract = signed_contract
    adversarial = copy.deepcopy(contract)
    adversarial["cidrs"] = cidrs
    resign(adversarial, private_key)
    with pytest.raises(ValueError, match="host routes|empty"):
        contract_module.verify_contract(
            adversarial, private_key.public_key(), now=NOW, resolver=resolver
        )


def test_rejects_missing_set_even_with_valid_signature(signed_contract):
    private_key, contract = signed_contract
    adversarial = copy.deepcopy(contract)
    del adversarial["cidrs"]
    resign(adversarial, private_key)
    with pytest.raises(ValueError, match="missing"):
        contract_module.verify_contract(
            adversarial, private_key.public_key(), now=NOW, resolver=resolver
        )


def test_rejects_signed_non_provider_resolution(signed_contract):
    private_key, contract = signed_contract
    adversarial = copy.deepcopy(contract)
    adversarial["resolutions"]["cpl.iam.api.nebius.cloud"] = ["203.0.113.44"]
    adversarial["cidrs"] = ["198.51.100.11/32", "203.0.113.44/32", "2001:db8::12/128"]
    resign(adversarial, private_key)
    with pytest.raises(ValueError, match="live Nebius"):
        contract_module.verify_contract(
            adversarial, private_key.public_key(), now=NOW, resolver=resolver
        )


def test_live_network_policy_must_equal_contract_with_explicit_to_and_tcp_443(
    signed_contract,
):
    _, contract = signed_contract
    policy = {
        "spec": {
            "egress": [
                {
                    "to": [{"ipBlock": {"cidr": cidr}} for cidr in contract["cidrs"]],
                    "ports": [{"port": 443, "protocol": "TCP"}],
                }
            ]
        }
    }
    contract_module.verify_network_policy(policy, contract["cidrs"])
    policy["spec"]["egress"][0]["ports"] = [{"port": 443}]
    with pytest.raises(ValueError, match="TCP/443"):
        contract_module.verify_network_policy(policy, contract["cidrs"])


def test_live_network_policy_rejects_extra_https_rule_or_non_ip_peer(signed_contract):
    _, contract = signed_contract
    exact = {
        "to": [{"ipBlock": {"cidr": cidr}} for cidr in contract["cidrs"]],
        "ports": [{"port": 443, "protocol": "TCP"}],
    }
    policy = {"spec": {"egress": [copy.deepcopy(exact)]}}
    policy["spec"]["egress"][0]["to"].append(
        {"namespaceSelector": {"matchLabels": {"unreviewed": "true"}}}
    )
    with pytest.raises(ValueError, match="exact IP blocks"):
        contract_module.verify_network_policy(policy, contract["cidrs"])

    policy = {
        "spec": {
            "egress": [
                exact,
                {
                    "to": [{"ipBlock": {"cidr": "203.0.113.1/32"}}],
                    "ports": [{"port": 443, "protocol": "TCP"}],
                },
            ]
        }
    }
    with pytest.raises(ValueError, match="does not equal"):
        contract_module.verify_network_policy(policy, contract["cidrs"])


def test_readiness_requires_exact_dns_database_provider_and_api_rules(signed_contract):
    _, contract = signed_contract
    provider_rule = {
        "to": [{"ipBlock": {"cidr": cidr}} for cidr in contract["cidrs"]],
        "ports": [{"port": 443, "protocol": "TCP"}],
    }
    api_rule = {
        "to": [{"ipBlock": {"cidr": "192.0.2.1/32"}}],
        "ports": [{"port": 443, "protocol": "TCP"}],
    }
    dns_rule = {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                },
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/instance": "coredns",
                        "app.kubernetes.io/name": "coredns",
                        "k8s-app": "coredns",
                    }
                },
            }
        ],
        "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
    }
    database_rule = {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "fs2-data"}
                },
                "podSelector": {"matchLabels": {"cnpg.io/cluster": "fs2-control-db"}},
            }
        ],
        "ports": [{"port": 5432, "protocol": "TCP"}],
    }
    policy = {"spec": {"egress": [dns_rule, database_rule, provider_rule, api_rule]}}
    contract_module.verify_network_policy(policy, contract["cidrs"], ["192.0.2.1/32"])

    for mutation in ("destinationless", "dns-selector", "extra-rule"):
        adversarial = copy.deepcopy(policy)
        if mutation == "destinationless":
            del adversarial["spec"]["egress"][2]["to"]
        elif mutation == "dns-selector":
            adversarial["spec"]["egress"][0]["to"][0]["podSelector"]["matchLabels"][
                "k8s-app"
            ] = "kube-dns"
        else:
            adversarial["spec"]["egress"].append(copy.deepcopy(provider_rule))
        with pytest.raises(ValueError):
            contract_module.verify_network_policy(
                adversarial, contract["cidrs"], ["192.0.2.1/32"]
            )


def test_effective_union_rejects_any_broad_selecting_policy(signed_contract):
    _, contract = signed_contract
    labels = {
        "app.kubernetes.io/name": "fs2-serve-control-plane",
        "app.kubernetes.io/instance": "fs2-serve-control-plane",
        "app.kubernetes.io/component": "storage-reconciler-v3",
        "fs2.nebius.ai/storage-egress-generation": "g20260916180000-aaaaaaaaaaaa",
        "fs2.nebius.ai/storage-rollout-generation": "r20260916180000-bbbbbbbbbbbb",
        "pod-template-hash": "6f9f7b8d7c",
    }
    canonical_rules = contract_module._canonical_egress_rules(
        contract["cidrs"], ["192.0.2.1/32"]
    )
    exact = {
        "metadata": {"name": "fs2-customer-storage-egress-generation"},
        "spec": {
            "podSelector": {
                "matchLabels": {
                    key: value
                    for key, value in labels.items()
                    if key
                    not in {
                        "fs2.nebius.ai/storage-rollout-generation",
                        "pod-template-hash",
                    }
                }
            },
            "policyTypes": ["Ingress", "Egress"],
            "egress": canonical_rules,
        },
    }
    deny_only = {
        "metadata": {"name": "namespace-default-deny"},
        "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": []},
    }
    contract_module.verify_effective_network_policies(
        [exact, deny_only], labels, contract["cidrs"], ["192.0.2.1/32"]
    )

    broad = {
        "metadata": {"name": "retained-broad-policy"},
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "fs2-serve-control-plane"}},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [{"ipBlock": {"cidr": "0.0.0.0/1"}}],
                    "ports": [{"port": 443, "protocol": "TCP"}],
                }
            ],
        },
    }
    with pytest.raises(ValueError, match="widens"):
        contract_module.verify_effective_network_policies(
            [exact, broad], labels, contract["cidrs"], ["192.0.2.1/32"]
        )


def test_effective_union_requires_one_exact_generation_selector(signed_contract):
    _, contract = signed_contract
    labels = {
        "app.kubernetes.io/name": "fs2-serve-control-plane",
        "app.kubernetes.io/instance": "fs2-serve-control-plane",
        "app.kubernetes.io/component": "storage-reconciler-v3",
        "fs2.nebius.ai/storage-egress-generation": "g20260916180000-aaaaaaaaaaaa",
        "fs2.nebius.ai/storage-rollout-generation": "r20260916180000-bbbbbbbbbbbb",
        "pod-template-hash": "6f9f7b8d7c",
    }
    rules = contract_module._canonical_egress_rules(contract["cidrs"], ["192.0.2.1/32"])
    overmatching = {
        "spec": {
            "podSelector": {
                "matchLabels": {"app.kubernetes.io/component": "storage-reconciler-v3"}
            },
            "policyTypes": ["Egress"],
            "egress": rules,
        }
    }
    with pytest.raises(ValueError, match="exactly one generation policy"):
        contract_module.verify_effective_network_policies(
            [overmatching], labels, contract["cidrs"], ["192.0.2.1/32"]
        )


@pytest.mark.parametrize("operator,values", [("Exists", []), ("In", ["6f9f7b8d7c"])])
def test_runtime_hash_selector_cannot_hide_widening_policy(
    signed_contract, operator, values
):
    _, contract = signed_contract
    labels = {
        "app.kubernetes.io/name": "fs2-serve-control-plane",
        "app.kubernetes.io/instance": "fs2-serve-control-plane",
        "app.kubernetes.io/component": "storage-reconciler-v3",
        "fs2.nebius.ai/storage-egress-generation": "g20260916180000-aaaaaaaaaaaa",
        "fs2.nebius.ai/storage-rollout-generation": "r20260916180000-bbbbbbbbbbbb",
        "pod-template-hash": "6f9f7b8d7c",
    }
    exact = {
        "spec": {
            "podSelector": {
                "matchLabels": {
                    key: value
                    for key, value in labels.items()
                    if key
                    not in {
                        "fs2.nebius.ai/storage-rollout-generation",
                        "pod-template-hash",
                    }
                }
            },
            "policyTypes": ["Ingress", "Egress"],
            "egress": contract_module._canonical_egress_rules(
                contract["cidrs"], ["192.0.2.1/32"]
            ),
        }
    }
    widening = {
        "spec": {
            "podSelector": {
                "matchExpressions": [
                    {
                        "key": "pod-template-hash",
                        "operator": operator,
                        "values": values,
                    }
                ]
            },
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [{"ipBlock": {"cidr": "0.0.0.0/1"}}],
                    "ports": [{"port": 443, "protocol": "TCP"}],
                }
            ],
        }
    }
    with pytest.raises(ValueError, match="widens"):
        contract_module.verify_effective_network_policies(
            [exact, widening], labels, contract["cidrs"], ["192.0.2.1/32"]
        )


def test_notin_selector_matches_missing_key_and_cannot_hide_broad_egress(
    signed_contract,
):
    _, contract = signed_contract
    labels = {
        "app.kubernetes.io/name": "fs2-serve-control-plane",
        "app.kubernetes.io/instance": "fs2-serve-control-plane",
        "app.kubernetes.io/component": "storage-reconciler-v3",
        "fs2.nebius.ai/storage-egress-generation": "g20260916180000-aaaaaaaaaaaa",
        "fs2.nebius.ai/storage-rollout-generation": "r20260916180000-bbbbbbbbbbbb",
        "pod-template-hash": "6f9f7b8d7c",
    }
    rules = contract_module._canonical_egress_rules(contract["cidrs"], ["192.0.2.1/32"])
    exact = {
        "spec": {
            "podSelector": {
                "matchLabels": {
                    key: value
                    for key, value in labels.items()
                    if key
                    not in {
                        "fs2.nebius.ai/storage-rollout-generation",
                        "pod-template-hash",
                    }
                }
            },
            "policyTypes": ["Ingress", "Egress"],
            "egress": rules,
        }
    }
    negative = {
        "spec": {
            "podSelector": {
                "matchExpressions": [
                    {"key": "unrelated-key", "operator": "NotIn", "values": ["blocked"]}
                ]
            },
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [{"ipBlock": {"cidr": "0.0.0.0/1"}}],
                    "ports": [{"port": 443, "protocol": "TCP"}],
                }
            ],
        }
    }

    assert contract_module._selector_matches(
        negative["spec"]["podSelector"], labels
    )
    with pytest.raises(ValueError, match="widens"):
        contract_module.verify_effective_network_policies(
            [exact, negative], labels, contract["cidrs"], ["192.0.2.1/32"]
        )

    excludes = {
        "matchExpressions": [
            {
                "key": "app.kubernetes.io/component",
                "operator": "NotIn",
                "values": ["storage-reconciler-v3"],
            }
        ]
    }
    assert not contract_module._selector_matches(excludes, labels)


def cli_inputs(tmp_path: Path, *, observed_at: datetime | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    contract = contract_module.create_contract(
        private_key,
        now=observed_at or datetime.now(UTC),
        resolver=resolver,
    )
    contract_path = tmp_path / "contract.json"
    public_key_path = tmp_path / "public-key.pem"
    cidrs_path = tmp_path / "cidrs.json"
    policy_path = tmp_path / "network-policy.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    cidrs_path.write_text(json.dumps(contract["cidrs"]), encoding="utf-8")
    policy_path.write_text(
        json.dumps(
            {
                "spec": {
                    "egress": [
                        {
                            "to": [
                                {"ipBlock": {"cidr": cidr}}
                                for cidr in contract["cidrs"]
                            ],
                            "ports": [{"port": 443, "protocol": "TCP"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return (
        private_key,
        contract,
        contract_path,
        public_key_path,
        cidrs_path,
        policy_path,
    )


def run_verify_cli(
    monkeypatch,
    contract: Path,
    public_key: Path,
    cidrs: Path,
    policy: Path | None = None,
) -> int:
    argv = [
        "customer-storage-egress-contract",
        "--contract",
        str(contract),
        "--public-key",
        str(public_key),
        "--expected-cidrs",
        str(cidrs),
    ]
    if policy is not None:
        argv.extend(("--network-policy", str(policy)))
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(contract_module.socket, "getaddrinfo", resolver)
    return contract_module.main()


def test_direct_rollout_cli_rejects_expired_and_forged_contracts(tmp_path, monkeypatch):
    _, _, contract_path, public_key_path, cidrs_path, _ = cli_inputs(
        tmp_path,
        observed_at=datetime.now(UTC) - timedelta(days=2),
    )
    assert run_verify_cli(monkeypatch, contract_path, public_key_path, cidrs_path) == 2

    private_key, contract, contract_path, public_key_path, cidrs_path, _ = cli_inputs(
        tmp_path / "forged"
    )
    del private_key
    contract["signature"] = base64.b64encode(b"0" * 64).decode()
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    assert run_verify_cli(monkeypatch, contract_path, public_key_path, cidrs_path) == 2


@pytest.mark.parametrize("linked_input", ["contract", "public_key", "cidrs", "policy"])
def test_direct_rollout_cli_rejects_symlinked_inputs(
    tmp_path, monkeypatch, linked_input
):
    _, _, contract_path, public_key_path, cidrs_path, policy_path = cli_inputs(tmp_path)
    paths = {
        "contract": contract_path,
        "public_key": public_key_path,
        "cidrs": cidrs_path,
        "policy": policy_path,
    }
    target = paths[linked_input]
    real = target.with_suffix(target.suffix + ".real")
    target.rename(real)
    target.symlink_to(real)
    assert (
        run_verify_cli(
            monkeypatch, contract_path, public_key_path, cidrs_path, policy_path
        )
        == 2
    )


def test_direct_rollout_cli_rejects_reassigned_provider_address(tmp_path, monkeypatch):
    _, _, contract_path, public_key_path, cidrs_path, _ = cli_inputs(tmp_path)

    def reassigned(host: str, port: int, *, type: int):
        values = resolver(host, port, type=type)
        if host == "cpl.iam.api.nebius.cloud":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.99", 443))]
        return values

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "customer-storage-egress-contract",
            "--contract",
            str(contract_path),
            "--public-key",
            str(public_key_path),
            "--expected-cidrs",
            str(cidrs_path),
        ],
    )
    monkeypatch.setattr(contract_module.socket, "getaddrinfo", reassigned)
    assert contract_module.main() == 2
