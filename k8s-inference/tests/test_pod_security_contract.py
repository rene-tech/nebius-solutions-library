from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_application_namespaces_enforce_baseline_with_restricted_reporting() -> None:
    foundation = _source("stages/foundation/locals.tf")
    for namespace in ("fs2-data", "fs2-models", "fs2-observability", "fs2-system"):
        assert f'"{namespace}",' in foundation
    assert 'var.pod_security_rollout_phase == "enforce"' in foundation
    assert '"pod-security.kubernetes.io/enforce" = "baseline"' in foundation
    assert '"pod-security.kubernetes.io/audit"   = "restricted"' in foundation
    assert '"pod-security.kubernetes.io/warn"    = "restricted"' in foundation

    namespaces = _source("stages/foundation/namespaces.tf")
    assert "lookup(local.pod_security_labels, each.value, {})" in namespaces
    assert "lookup(local.pod_security_annotations, each.value, {})" in namespaces

    academic = _source("modules/academic-assets/main.tf")
    modelexpress = _source("stages/workloads/modelexpress.tf")
    for source in (academic, modelexpress):
        assert "pod_security" in source
        assert '"pod-security.kubernetes.io/enforce" = "baseline"' in source
        assert '"pod-security.kubernetes.io/audit"   = "restricted"' in source
        assert '"pod-security.kubernetes.io/warn"    = "restricted"' in source


def test_host_integrated_agents_are_isolated_in_an_annotated_exception_namespace() -> None:
    foundation = _source("stages/foundation/locals.tf")
    assert '"fs2-node-observability" = tomap({' in foundation
    assert '"pod-security.kubernetes.io/enforce" = "privileged"' in foundation
    assert (
        '"security.fs2.nebius.ai/pod-security-exception" = '
        '"node-observability-host-integration"'
    ) in foundation

    releases = _source("stages/foundation/releases.tf")
    workloads = _source("stages/workloads/observability.tf")
    control_plane = _source("stages/workloads/control_plane.tf")
    assert "namespaceOverride = kubernetes_namespace_v1.platform[local.node_observability_namespace]" in releases
    assert "namespace        = kubernetes_namespace_v1.platform[local.node_observability_namespace]" in releases
    assert '"fs2-node-observability" :\n    "fs2-observability"' in workloads
    assert "daemonSetNamespace = local.gpu_observer_namespace" in control_plane


def test_rollout_and_rollback_order_are_gated_by_readiness_receipts() -> None:
    foundation = _source("stages/foundation/locals.tf")
    foundation_contract = _source("stages/foundation/pod_security.tf")
    workloads = _source("stages/workloads/observability.tf")
    workloads_contract = _source("stages/workloads/pod_security.tf")

    for phase in (
        '"prepare"',
        '"migrate-reference-data"',
        '"enforce"',
        '"rollback-restore-host-agents"',
        '"rollback-remove-exception"',
    ):
        assert phase in _source("stages/foundation/variables.tf")
        assert phase in _source("stages/workloads/variables.tf")

    assert 'var.pod_security_rollout_phase == "enforce"' in foundation
    assert (
        'var.pod_security_rollout_phase != "rollback-remove-exception"'
        in foundation
    )
    assert '"fs2-node-observability" :\n    "fs2-observability"' in foundation
    assert '"fs2-node-observability" :\n    "fs2-observability"' in workloads
    assert '"fs2-node-observability" :\n    "fs2-system"' in workloads
    for contract in (foundation_contract, workloads_contract):
        assert "pod_security_host_agent_readiness_receipt_sha256 != null" in contract
        assert "pod_security_host_agent_restore_receipt_sha256 != null" in contract


def test_reference_data_moves_to_verified_rwx_csi_before_baseline() -> None:
    reference_data = _source("reference-data/terraform/main.tf")
    variables = _source("reference-data/terraform/variables.tf")
    assert 'storage_class = optional(string, "csi-mounted-fs-path-sc")' in variables
    assert 'access_modes       = ["ReadWriteMany"]' in reference_data
    assert 'persistent_volume_claim {' in reference_data
    assert 'var.pod_security_rollout_phase == "enforce" ? "baseline" : "privileged"' in reference_data
    assert 'var.csi_migration_receipt != null' in reference_data
    assert 'var.csi_readiness_receipt_sha256 != null' in reference_data
    assert '"pod-security.kubernetes.io/audit"   = "restricted"' in reference_data
    assert '"pod-security.kubernetes.io/warn"    = "restricted"' in reference_data
    assert 'local.csi_storage_enabled ? [true] : []' in reference_data
    assert 'local.csi_storage_enabled ? [] : [true]' in reference_data


def test_live_scientific_namespace_inventory_is_complete_and_label_only() -> None:
    workload = _source("stages/workloads/pod_security.tf")
    overlay = _source("examples/pod-security-live-scientific-namespaces.tfvars")
    assert 'resource "kubernetes_labels" "existing_scientific_pod_security"' in workload
    assert 'kind        = "Namespace"' in workload
    assert "force  = false" in workload
    for namespace in (
        "fs2-bioir-boltz2",
        "fs2-bioir-coverage",
        "fs2-bioir-openfold",
        "fs2-bioir-protenix",
        "fs2-bioir-snapshot",
    ):
        assert f'"{namespace}"' in overlay


def test_dynamic_controller_cannot_discover_or_own_node_agents_or_identities() -> None:
    renderer = _source("components/control-plane/src/fs2_serve/model_deployment.py")
    controller = _source("components/control-plane/src/fs2_serve/model_deployment_controller.py")
    workloads = _source("stages/workloads/model_controller.tf")
    control_plane = _source("stages/workloads/control_plane.tf")

    allowed_gvks = renderer.split("_ALLOWED_TEMPLATE_GVKS = frozenset(", 1)[1].split(")\n", 1)[0]
    endpoints = controller.split("RESOURCE_ENDPOINTS = {", 1)[1].split("}\n", 1)[0]
    supported_gvks = workloads.split("model_controller_supported_template_gvks = toset([", 1)[1].split("])", 1)[0]
    for source in (allowed_gvks, endpoints, supported_gvks):
        assert "ServiceAccount" not in source
        assert "DaemonSet" not in source
        assert "NetworkPolicy" not in source
    assert 'if mechanism != "hostMemoryResidency"' in workloads
    assert "model_controller_network_policy_resource_names" not in workloads
    assert 'toset(["v1/ServiceAccount"])' in workloads
    models = _source("stages/workloads/models.tf")
    assert 'resource "kubernetes_service_account_v1" "model_runtime"' in models
    assert 'name      = "fs2-model-runtime"' in models
    assert "automount_service_account_token = false" in models
    assert "networkPolicyResourceNames" not in control_plane


def test_model_controller_has_no_network_policy_api_or_rbac_authority() -> None:
    rbac = _source("charts/control-plane/fs2-serve-control-plane/templates/model-controller-rbac.yaml")
    controller = _source("components/control-plane/src/fs2_serve/model_deployment_controller.py")
    renderer = _source("components/control-plane/src/fs2_serve/model_deployment.py")
    assert "networkpolicies" not in rbac
    assert '"NetworkPolicy"' not in controller.split("RESOURCE_ENDPOINTS = {", 1)[1].split("}\n", 1)[0]
    assert '"kind": "NetworkPolicy"' not in renderer
