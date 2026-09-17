from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCIENTIFIC_NAMESPACES = (
    "fs2-academic-poc",
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
        "stages/foundation/pod_security.tf",
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
    assert "pod_security_exception_manager_usernames" not in admission
    assert "request.userInfo.username" in admission
    assert "Only the Kubernetes DaemonSet controller may create host-agent Pods" in admission
    assert "exact reviewed image, command, service account" in admission
    assert "fs2-pod-security-rollout-custodian" in admission
    assert 'resource "kubernetes_role_binding_v1" "pod_security_rollout_ledger"' in admission
    assert 'resource "kubernetes_role_binding_v1" "pod_security_rollout_custodian_ledger"' in admission
    assert (
        'resource "kubernetes_cluster_role_binding_v1" "pod_security_rollout_custodian_reader"'
        in admission
    )
    assert '"podtemplates"' in admission
    assert 'resources   = ["pods/ephemeralcontainers"]' in admission
    assert "Ephemeral containers are forbidden" in admission
    assert "v.hostPath == {'path':'/'}" in admission
    assert "m.mountPath == '/host/root' && m.mountPropagation == 'HostToContainer' && m.readOnly == true" in admission
    assert "object.spec.template.spec.hostIPC == false" in admission
    assert "quay.io/prometheus/node-exporter:v1.12.1@sha256:" in _source("locals.tf")
    assert "containerSecurityContext = {" in _source("stages/foundation/releases.tf")
    assert "object.spec.template.spec.securityContext == {'fsGroup':65534" in admission
    assert "object.spec.template.spec.securityContext == {'fsGroup':10001" in admission
    assert "object.metadata.name == 'fs2-dcgm-exporter'" in admission
    assert "c.securityContext.capabilities.add == ['SYS_ADMIN']" in admission
    assert "v.name.startsWith('kube-api-access-')" in admission
    assert "!has(e.valueFrom.secretKeyRef)" in admission
    assert "v.projected == {'defaultMode':256" in admission
    assert "object.spec.template.spec.containers[0].image == '%s'" in admission
    assert "object.spec.template.spec.containers[0].command == ['/otelcol-k8s']" in admission
    for name in (
        "fs2-dcgm-exporter",
        "fs2-node-exporter",
        "fs2-otel-node-agent",
        "fs2-serve-control-plane-gpu-observer",
    ):
        assert f'"{name}"' in admission


def test_rollout_gate_consumes_prior_signed_state_without_phase_skips() -> None:
    verifier = _source("scripts/verify_pod_security_receipts.py")
    gate = _source("modules/pod-security-rollout-gate/main.tf")
    admission = _source("stages/foundation/pod_security_admission.tf")
    expected = {
        "bootstrap-baseline": "baseline-captured",
        "migrate-reference-data": "exception-ready",
        "cleanup-legacy-resources": "reference-data-ready",
        "quiesce-enforcement": "enforcement-quiesced",
        "enforce": "baseline-enforced",
        "rollback-remove-enforcement": "enforcement-removed",
        "rollback-restore-host-agents": "host-agents-restored",
        "rollback-remove-exception": "rolled-back",
    }
    for phase, terminal in expected.items():
        assert f'"{phase}"' in verifier
        assert f'"{terminal}"' in verifier
    assert "Ed25519 signature" in verifier
    assert "whole-bundle signature verification failed" in verifier
    assert "prior_ledger_sha256" in verifier
    assert "resourceVersion" in verifier
    assert "_validate_live_observations" in verifier
    assert "_validate_baseline_artifact" in verifier
    assert "_validate_cleanup_result" in verifier
    assert '"create"' in verifier and '"token"' in verifier
    assert "--duration=10m" in verifier
    assert "https://kubernetes.default.svc" in verifier
    assert "--as=" not in verifier
    assert "SelfSubjectAccessReview" in verifier
    assert "SelfSubjectRulesReview" in verifier
    assert "SelfSubjectReview" in verifier
    assert "external custody and platform Terraform must not share a kubeconfig" in verifier
    assert "external custody has a persisted mutation, RBAC, admission, or workload pivot" in verifier
    assert "ambient identity has direct rollout-ledger authority" in verifier
    assert "ambient identity can dismantle rollout-ledger admission" in verifier
    assert "ambient identity may impersonate service accounts" in verifier
    assert "ambient identity may forge authenticator credential metadata" in verifier
    assert "os.memfd_create" in verifier
    assert "FS2_PLATFORM_KUBECONFIG" in gate
    assert "FS2_POD_SECURITY_CUSTODY_USER" in gate
    assert "foundation resources have not acknowledged this authorization" in verifier
    assert "prior phase has not been acknowledged by both Terraform stages" in verifier
    assert '"owner-acknowledgement"' in verifier
    assert '"downstream-acknowledgement"' in verifier
    assert 'resource "kubernetes_config_map_v1" "ledger"' in gate
    assert "prevent_destroy = true" in gate
    assert "ignore_changes  = [data]" in gate
    assert 'resource "terraform_data" "verified"' in gate
    assert "baseline_artifact_path" in gate
    assert 'data "external"' not in gate
    assert 'operations  = ["UPDATE", "DELETE"]' in admission
    assert "The monotonic pod-security rollout ledger may not be deleted" in admission
    assert "object.data.size() == 19" in admission
    assert "pod-security-rollout-ledger/v3" in admission
    assert 'resources      = ["persistentvolumes"]' in admission
    assert "pod_security_rollout_persistent_volumes" in admission
    assert "proof_generation_ids" in admission
    assert "authentication.kubernetes.io/credential-id" in admission
    assert "serviceaccounts/token" in admission
    assert "fs2-pod-security-receipt-custodians" in admission
    assert "directly authenticated external OIDC identity" in admission
    token_binding = admission.split(
        'resource "kubernetes_manifest" "pod_security_rollout_token_binding"', 1
    )[1].split('resource "kubernetes_manifest" "pod_security_enforcement_fence_policy"', 1)[0]
    cleanup_binding = admission.split(
        'resource "kubernetes_manifest" "pod_security_legacy_cleanup_fence_binding"', 1
    )[1].split('resource "kubernetes_manifest" "pod_security_ledger_policy"', 1)[0]
    assert '"kubernetes.io/metadata.name" = "fs2-system"' in token_binding
    assert '"kubernetes.io/metadata.name" = "fs2-models"' in cleanup_binding
    assert "pod-security-rollout-ledger/v2" in admission
    assert '"fresh-v3"' in _source("stages/workloads/variables.tf")
    assert '"retained-v2"' in _source("stages/workloads/variables.tf")
    assert "predecessor_adoption" in _source("stages/workloads/reference_data_successors.tf")
    assert "Proof-generation custody may append only with a phase transition" in admission
    assert "int(object.data.sequence) == int(oldObject.data.sequence) + 1" in admission
    assert "authorization_owner_acknowledged == 'false'" in admission
    assert "authorization_downstream_acknowledged == 'false'" in admission
    assert "fs2-pod-security-enforcement-fence" in admission
    assert "authorization_downstream_acknowledged != 'true'" in admission
    assert "Pods owned by a retained legacy DaemonSet are permanently quarantined" in admission
    assert "TokenRequest is forbidden for every retained legacy ServiceAccount" in admission

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
        assert '"bootstrap-baseline"' in source
        assert '"rollback-restore-host-agents"' in source
        assert '"rollback-remove-exception"' in source
    assert 'resource "helm_release" "node_exporter_exception"' in foundation
    assert 'resource "helm_release" "otel_node_exception"' in foundation
    assert 'resource "helm_release" "dcgm_exporter_exception"' in workloads
    assert "additionalDaemonSetNamespaces = local.gpu_observer_additional_namespaces" in control_plane


def test_quiesce_acknowledgement_rechecks_clean_inventory_after_fence_cas() -> None:
    verifier = _source("scripts/verify_pod_security_receipts.py")
    assert 'mode in {"owner-acknowledgement", "downstream-authorization"}' in verifier
    assert 'and observation_state == "cleanup-complete"' in verifier
    assert 'observation_state in {"cleanup-complete", "enforcement-quiesced"}' in verifier


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
    assert 'resource "kubernetes_job_v1" "csi_read_probe"' in reference
    assert '"--pvc-resource-version"' in reference
    assert '"--volume-name"' in reference
    assert '"--challenge"' in reference
    assert '"--proof-output", "/dev/termination-log"' in reference
    assert "verify_checkpoint_durability.py" in reference
    assert "read_only  = true" in reference
    assert "automount_service_account_token = false" in reference
    assert "csi_migration_receipt" not in variables
    assert "csi_readiness_receipt_sha256" not in variables


def test_non_test_iac_owns_every_retained_storage_successor_and_exact_proof() -> None:
    source = _source("stages/workloads/reference_data_successors.tf")
    variables = _source("variables.tf")
    verifier = _source("scripts/verify_pod_security_receipts.py")

    for namespace in (
        "fs2-bioir-boltz2",
        "fs2-bioir-coverage",
        "fs2-bioir-openfold",
        "fs2-bioir-protenix",
        "fs2-bioir-snapshot",
        "fs2-snapshot-operations",
    ):
        assert namespace in source
    for identity in (
        "fs2-snapshot-reference",
        "fs2-snapshot-checkpoints",
        "fs2-snapshot-checkpoints-retained-sc",
    ):
        assert identity in source

    assert source.count('resource "kubernetes_persistent_volume_v1"') == 2
    assert source.count('resource "kubernetes_persistent_volume_claim_v1"') == 2
    assert 'resource "kubernetes_config_map_v1" "pod_security_successor_tools_adopted"' in source
    assert 'resource "kubernetes_config_map_v1" "pod_security_successor_tools_generation"' in source
    assert 'resource "kubernetes_job_v1" "pod_security_reference_successor_probe_adopted"' in source
    assert 'resource "kubernetes_job_v1" "pod_security_reference_successor_probe_generation"' in source
    assert 'resource "kubernetes_job_v1" "pod_security_snapshot_checkpoint_write_generation"' in source
    assert 'resource "kubernetes_job_v1" "pod_security_snapshot_checkpoint_read_generation"' in source
    assert source.count("prevent_destroy = true") >= 8
    assert "pod_security_successor_tool_instances" in source
    assert "pod_security_reference_probe_instances" in source
    assert "pod_security_predecessor_resources" in source
    assert source.count("moved {") >= 14
    assert '"${generation_id}/${successor_key}"' in source
    assert "ignore_changes  = all" in source
    assert "backoff_limit           = 2" in source
    assert '"--generation"' in source
    assert '"--attempt"' in source
    assert "proof_generation_ledger" in variables
    assert "maximum_generations == 8" in variables
    assert 'sai07-baseline-inventory/v3' not in verifier
    assert 'persistent_volume_reclaim_policy = "Retain"' in source
    assert 'reclaim_policy      = "Retain"' in source
    assert 'read_only         = true' in source
    assert 'volume_handle     = var.pod_security_successor_storage.reference_source.volume_handle' in source
    assert 'volume_handle     = var.pod_security_successor_storage.checkpoint_source.volume_handle' in source
    assert 'depends_on = [kubernetes_job_v1.pod_security_snapshot_checkpoint_write_generation[each.key]]' in source
    assert '"--pvc-resource-version"' in source
    assert '"--proof-output", "/dev/termination-log"' in source
    assert 'successor_storage = optional(object({' in variables
    assert '"successor_storage_sha256"' in verifier


def test_successor_storage_is_exactly_signed_and_cannot_fall_back_to_dynamic_empty_claims() -> None:
    source = _source("stages/workloads/reference_data_successors.tf")
    workloads = _source("stages/workloads/pod_security.tf")
    foundation = _source("stages/foundation/pod_security.tf")

    assert 'data "kubernetes_persistent_volume_v1" "pod_security_reference_source"' in source
    assert '.persistent_volume_source[0].csi[0].volume_handle ==' in source
    assert '.persistent_volume_source[0].csi[0].volume_attributes ==' in source
    assert 'storage_class_name = "fs2-reference-data-retained-sc"' in source
    assert 'volume_name        = kubernetes_persistent_volume_v1.pod_security_reference_successor' in source
    assert "generate_name" not in source
    assert 'successor_storage_sha256 = (' in workloads
    assert 'successor_storage_sha256 = var.pod_security_successor_storage_sha256' in foundation


def test_scientific_inventory_is_exact_nonempty_and_scanned_before_enforcement() -> None:
    root_variables = _source("variables.tf")
    stage_variables = _source("stages/workloads/variables.tf")
    scanner = _source("scripts/audit_sai07_baseline_inventory.py")
    for namespace in SCIENTIFIC_NAMESPACES:
        assert f'"{namespace}"' in root_variables
        assert f'"{namespace}"' in stage_variables
        assert f'"{namespace}"' in scanner
    assert "live scientific namespace inventory differs" in scanner
    assert '"reference_host_paths"' in scanner
    assert '"baseline_incompatible_objects"' in scanner
    assert '"restricted_incompatible_objects"' in scanner
    for kind in ("ReplicationController", "ReplicaSet", "JobSet", "ModelDeployment", "ScaledObject"):
        assert f'"{kind}"' in scanner
    assert '"unauthorized_exception_objects"' in scanner
    assert 'resource "kubernetes_labels" "existing_scientific_pod_security"' in _source(
        "stages/workloads/pod_security.tf"
    )


def test_dynamic_controller_has_zero_configmap_networkpolicy_pvc_serviceaccount_or_daemonset_authority() -> None:
    renderer = _source("components/control-plane/src/fs2_serve/model_deployment.py")
    controller = _source("components/control-plane/src/fs2_serve/model_deployment_controller.py")
    workloads = _source("stages/workloads/model_controller.tf")
    rbac = _source("charts/control-plane/fs2-serve-control-plane/templates/model-controller-rbac.yaml")

    allowed_gvks = renderer.split("_ALLOWED_TEMPLATE_GVKS = frozenset(", 1)[1].split(")\n", 1)[0]
    endpoints = controller.split("RESOURCE_ENDPOINTS = {", 1)[1].split("}\n", 1)[0]
    supported = workloads.split("model_controller_supported_template_gvks = toset([", 1)[1].split("])", 1)[0]
    for source in (endpoints, supported):
        assert "ConfigMap" not in source
        assert "PersistentVolumeClaim" not in source
        assert "ServiceAccount" not in source
        assert "DaemonSet" not in source
        assert "NetworkPolicy" not in source
    # Qualified legacy templates may still describe a Terraform-owned PVC as
    # a mount dependency, but the renderer must discard that manifest before
    # discovery/adoption and the controller must have no corresponding API.
    assert 'if kind == "PersistentVolumeClaim":\n                continue' in renderer
    for resource in ("configmaps", "persistentvolumeclaims", "networkpolicies", "serviceaccounts", "daemonsets"):
        assert resource not in rbac
    assert "model_controller_network_policy_resource_names" not in workloads
    assert "networkPolicyResourceNames" not in _source("stages/workloads/control_plane.tf")


def test_immutable_telemetry_generations_are_retained_across_every_phase() -> None:
    foundation_locals = _source("stages/foundation/locals.tf")
    foundation_releases = _source("stages/foundation/releases.tf")
    workloads_observability = _source("stages/workloads/observability.tf")
    receipts = _source("scripts/verify_pod_security_receipts.py")

    assert "node_observability_exception_enabled = true" in foundation_locals
    assert 'toset(["fs2-observability", "fs2-node-observability"])' in foundation_releases
    for resource_name in ("dcgm_metrics", "dcgm_cold_config"):
        block = workloads_observability.split(
            f'resource "kubernetes_config_map_v1" "{resource_name}"', 1
        )[1].split("\n}\n", 1)[0]
        assert '"fs2-observability"' in block
        assert '"fs2-node-observability"' in block
        assert "node_agents_use_exception_namespace" not in block
    assert "prevent_destroy = true" in foundation_releases
    assert workloads_observability.count("prevent_destroy = true") >= 2
    assert "retained host-agent config is absent or mutable after rollback" in receipts


def test_functional_replacements_are_finite_tokenless_and_exactly_admitted() -> None:
    holders = _source("stages/workloads/fast_start_claims.tf")
    snapshot = _source("stages/foundation/pod_security_snapshot_admission.tf")
    assert 'resource "terraform_data" "fast_start_host_memory_contract"' in holders
    assert 'service_account_name = "fs2-model-runtime"' in holders
    assert "automountServiceAccountToken = false" in holders
    assert '"fs2-serve.nebius.ai/network-profile"      = "mounted-content"' in holders
    assert 'resource "kubernetes_manifest" "snapshot_pod_policy"' in snapshot
    assert "request.userInfo.username == '${local.snapshot_manager_username}'" in snapshot
    assert "object.spec.automountServiceAccountToken == false" in snapshot
    assert (
        "object.spec.hostNetwork == false && object.spec.hostPID == false && object.spec.hostIPC == false" in snapshot
    )
    assert "object.spec.containers[0].image == '${local.snapshot_runtime_image}'" in snapshot
    assert "object.spec.containers[0].command.size() in [14,18]" in snapshot
    assert "object.spec.ephemeralContainers.size() == 0" in snapshot
    assert "system:serviceaccount:kube-system:job-controller" in snapshot
    assert "snapshot_durability_pod_expression" in snapshot
    assert "snapshot_reference_probe_pod_expression" in snapshot
    assert "verify_checkpoint_durability.py" in snapshot
    assert "pod_security_storage_probe_image" in snapshot
    assert "pod_security_storage_tools_config_map" in snapshot


def test_legacy_cleanup_is_exactly_fenced_and_never_touches_finite_profiles() -> None:
    cleanup = _source("scripts/cleanup_sai07_legacy_resources.py")
    assert 'NAMESPACE = "fs2-models"' in cleanup
    assert "live_projection" in cleanup and '"object_sha256"' in cleanup
    assert "ServiceAccount" in cleanup and "DaemonSet" in cleanup and "NetworkPolicy" in cleanup
    assert 'item["name"].startswith("fs2-network-profile-")' in cleanup
    assert "Terraform-owned finite profile policies may never be cleaned" in cleanup
    assert "no-delete closure is blocked by retained legacy objects" in cleanup
    assert '"delete"' not in cleanup
    assert '"--execute"' not in cleanup
