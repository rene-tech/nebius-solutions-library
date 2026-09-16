from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCIENTIFIC_NAMESPACES = (
    "fs2-bioir-boltz2",
    "fs2-bioir-coverage",
    "fs2-bioir-openfold",
    "fs2-bioir-protenix",
    "fs2-bioir-snapshot",
)


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _assert_pinned_psa_labels(source: str) -> None:
    for key, value in (
        ("enforce", "baseline"),
        ("audit", "restricted"),
        ("warn", "restricted"),
    ):
        assert re.search(rf'"pod-security\.kubernetes\.io/{key}"\s*=\s*"{value}"', source)
        assert re.search(
            rf'"pod-security\.kubernetes\.io/{key}-version"\s*=\s*var\.pod_security_version',
            source,
        )


def test_every_baseline_owner_pins_enforce_audit_and_warn_minor() -> None:
    for relative in (
        "stages/foundation/locals.tf",
        "stages/workloads/pod_security.tf",
        "modules/academic-assets/main.tf",
        "stages/workloads/modelexpress.tf",
    ):
        _assert_pinned_psa_labels(_source(relative))

    reference = _source("reference-data/terraform/main.tf")
    assert re.search(
        r'"pod-security\.kubernetes\.io/enforce"\s*=\s*var\.pod_security_rollout_phase == "enforce" \? "baseline"',
        reference,
    )
    for key in ("enforce", "audit", "warn"):
        assert re.search(
            rf'"pod-security\.kubernetes\.io/{key}-version"\s*=\s*var\.pod_security_version',
            reference,
        )

    namespaces = _source("stages/foundation/namespaces.tf")
    assert "lookup(local.pod_security_labels, each.value, {})" in namespaces
    assert "lookup(local.pod_security_annotations, each.value, {})" in namespaces


def test_exception_namespace_has_enforceable_identity_and_content_admission() -> None:
    foundation = _source("stages/foundation/locals.tf")
    admission = _source("stages/foundation/pod_security_admission.tf")
    assert '"security.fs2.nebius.ai/host-agent-only" = "true"' in foundation
    assert re.search(r'"pod-security\.kubernetes\.io/enforce"\s*=\s*"privileged"', foundation)
    assert "pod_security_exception_manager_usernames" in admission
    assert "request.userInfo.username" in admission
    assert "Only the Kubernetes DaemonSet controller may create host-agent Pods" in admission
    assert "Only an explicitly reviewed rollout identity" in admission
    assert 'resources   = ["pods/ephemeralcontainers"]' in admission
    assert "Ephemeral containers are forbidden" in admission
    for name in (
        "fs2-dcgm-exporter",
        "fs2-node-exporter",
        "fs2-otel-node-agent",
        "fs2-serve-control-plane-gpu-observer",
    ):
        assert f'"{name}"' in admission


def test_rollout_gate_consumes_prior_signed_state_without_phase_skips() -> None:
    verifier = _source("scripts/verify_pod_security_receipts.py")
    expected = {
        "migrate-reference-data": "exception-ready",
        "cleanup-legacy-resources": "reference-data-ready",
        "enforce": "baseline-ready",
        "rollback-remove-enforcement": "baseline-enforced",
        "rollback-restore-host-agents": "enforcement-removed",
        "rollback-remove-exception": "host-agents-restored",
    }
    for phase, terminal in expected.items():
        assert f'"{phase}": "{terminal}"' in verifier
    assert "Ed25519 signature" in verifier
    assert "prior-state digest" in verifier
    assert "receipt sequence is not contiguous" in verifier
    assert "receipt state chain skips or reverses" in verifier

    reference = _source("reference-data/terraform/main.tf")
    for phase, terminal in expected.items():
        assert f'"{phase}"' in reference
        assert f'"{terminal}"' in reference
    assert "csi_migration_receipt" not in reference
    assert "csi_readiness_receipt_sha256" not in reference


def test_host_agents_dual_run_before_enforcement_and_restore_before_removal() -> None:
    foundation = _source("stages/foundation/releases.tf")
    workloads = _source("stages/workloads/observability.tf")
    control_plane = _source("stages/workloads/control_plane.tf")
    foundation_locals = _source("stages/foundation/locals.tf")
    for source in (foundation_locals, workloads):
        assert "legacy_host_agents_enabled" in source
        assert "exception_host_agents_enabled" in source
        assert '"prepare"' in source
        assert '"rollback-restore-host-agents"' in source
        assert '"rollback-remove-exception"' in source
    assert 'resource "helm_release" "node_exporter_exception"' in foundation
    assert 'resource "helm_release" "otel_node_exception"' in foundation
    assert 'resource "helm_release" "dcgm_exporter_exception"' in workloads
    assert "additionalDaemonSetNamespaces = local.gpu_observer_additional_namespaces" in control_plane


def test_reference_data_uses_only_dedicated_retained_rwx_csi() -> None:
    csi = _source("stages/workloads/reference_data_csi.tf")
    hardener = _source("stages/workloads/scripts/harden-reference-data-csi.py")
    reference = _source("reference-data/terraform/main.tf")
    variables = _source("reference-data/terraform/variables.tf")
    assert 'dataDir          = "/mnt/fs2-reference-data/csi-mounted-fs-path-data/"' in csi
    assert 'driverName       = "reference-data.mounted-fs-path.csi.nebius.ai"' in csi
    assert "prevent_destroy = true" in csi
    assert '"reclaimPolicy: Retain\\nvolumeBindingMode: WaitForFirstConsumer\\n"' in hardener
    assert 'storage_class = optional(string, "fs2-reference-data-retained-sc")' in variables
    assert 'access_modes       = ["ReadWriteMany"]' in reference
    assert "prevent_destroy = true" in reference
    assert "var.filesystem_claim.size_gib <= var.filesystem_claim.capacity_gib" in reference
    assert "csi_migration_receipt" not in variables
    assert "csi_readiness_receipt_sha256" not in variables


def test_scientific_inventory_is_exact_nonempty_and_scanned_before_enforcement() -> None:
    root_variables = _source("variables.tf")
    stage_variables = _source("stages/workloads/variables.tf")
    scanner = _source("scripts/audit_sai07_baseline_inventory.py")
    for namespace in SCIENTIFIC_NAMESPACES:
        assert f'"{namespace}"' in root_variables
        assert f'"{namespace}"' in stage_variables
        assert f'"{namespace}"' in scanner
    assert "live fs2-bioir namespace inventory differs" in scanner
    assert '"reference_host_paths"' in scanner
    assert '"baseline_incompatible_objects"' in scanner
    assert '"unauthorized_exception_objects"' in scanner
    assert 'resource "kubernetes_labels" "existing_scientific_pod_security"' in _source(
        "stages/workloads/pod_security.tf"
    )


def test_dynamic_controller_has_zero_networkpolicy_serviceaccount_or_daemonset_authority() -> None:
    renderer = _source("components/control-plane/src/fs2_serve/model_deployment.py")
    controller = _source("components/control-plane/src/fs2_serve/model_deployment_controller.py")
    workloads = _source("stages/workloads/model_controller.tf")
    rbac = _source("charts/control-plane/fs2-serve-control-plane/templates/model-controller-rbac.yaml")

    allowed_gvks = renderer.split("_ALLOWED_TEMPLATE_GVKS = frozenset(", 1)[1].split(")\n", 1)[0]
    endpoints = controller.split("RESOURCE_ENDPOINTS = {", 1)[1].split("}\n", 1)[0]
    supported = workloads.split("model_controller_supported_template_gvks = toset([", 1)[1].split("])", 1)[0]
    for source in (allowed_gvks, endpoints, supported):
        assert "ServiceAccount" not in source
        assert "DaemonSet" not in source
        assert "NetworkPolicy" not in source
    for resource in ("networkpolicies", "serviceaccounts", "daemonsets"):
        assert resource not in rbac
    assert "model_controller_network_policy_resource_names" not in workloads
    assert "networkPolicyResourceNames" not in _source("stages/workloads/control_plane.tf")


def test_legacy_cleanup_is_uid_fenced_and_never_touches_finite_profiles() -> None:
    cleanup = _source("scripts/cleanup_sai07_legacy_resources.py")
    assert 'NAMESPACE = "fs2-models"' in cleanup
    assert '"preconditions": {"uid": uid}' in cleanup
    assert "ServiceAccount" in cleanup and "DaemonSet" in cleanup and "NetworkPolicy" in cleanup
    assert 'item["name"].startswith("fs2-network-profile-")' in cleanup
    assert "Terraform-owned finite profile policies may never be cleaned" in cleanup
