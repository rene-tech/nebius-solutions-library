from __future__ import annotations

import json
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
    active_gate = gate.split("*/", 1)[1]
    handoff = _source("scripts/verify_sai07_external_handoff_v2.py")
    execution_ack = _source("scripts/verify_sai07_external_execution_ack_v3.py")
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
    assert "FS2_PLATFORM_KUBECONFIG" not in verifier
    assert "FS2_CUSTODY_OWNER_KUBECONFIG" not in verifier
    assert "external custody has authority outside namespace inventory, self-review, and exact token minting" in verifier
    assert "external custody has a non-discovery non-resource URL edge" in verifier
    assert "ambient identity has direct rollout-ledger authority" in verifier
    assert "ambient identity can dismantle rollout-ledger admission" in verifier
    assert "ambient identity may impersonate service accounts" in verifier
    assert "ambient identity may forge authenticator credential metadata" in verifier
    assert "os.memfd_create" in verifier
    assert "FS2_PLATFORM_KUBECONFIG" not in active_gate
    assert "FS2_POD_SECURITY_CUSTODY_USER" not in active_gate
    assert "FS2_CUSTODY_OWNER_KUBECONFIG" not in active_gate
    assert 'data "external" "verified_execution_acknowledgement"' in active_gate
    assert "verify_sai07_external_execution_ack_v3.py" in active_gate
    assert "verify_sai07_external_handoff_v2.py" not in active_gate
    assert "custody-trust-lock-v3.json" in active_gate
    assert "trust_lock_path" in active_gate
    assert "handoff_public_key_path" not in active_gate
    assert "authority_audits" in handoff
    assert "inactive-owner" in handoff
    assert "receipt-service-account" in handoff
    assert "metadata-reader" in handoff
    assert "self_subject_rules_reviews" in handoff
    assert "self_subject_access_reviews" in handoff
    assert "receipt_token_request" in handoff
    assert "metadata_token_request" in handoff
    assert "bound_object_ref" in handoff
    assert "foundation resources have not acknowledged this authorization" in verifier
    assert "prior phase has not been acknowledged by both Terraform stages" in verifier
    assert '"owner-acknowledgement"' in verifier
    assert '"downstream-acknowledgement"' in verifier
    assert 'resource "kubernetes_config_map_v1" "ledger"' not in active_gate
    assert 'resource "terraform_data" "verified"' in active_gate
    assert "external_acknowledgement_sha256" in active_gate
    assert "receipt_bundle_sha256" in active_gate
    assert "platform_objects_before_sha256" in execution_ack
    assert "platform_objects_after_sha256" in execution_ack
    assert "Terraform-retained objects changed" in execution_ack
    assert "--bound-object-kind=Secret" in verifier
    assert "FS2_POD_SECURITY_TOKEN_ANCHOR_UID" in verifier
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
    assert "fs2-pod-security-custody-boundary" in admission
    assert "fs2-pod-security-custody-owners" in admission
    assert "fs2-platform-terraform" in admission
    assert "system:masters" in admission
    assert "legacy ServiceAccount token Secrets" in admission

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
    assert 'sai07-baseline-inventory/v4' not in verifier
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
    assert "a live annotated legacy ServiceAccount token Secret blocks retained quarantine" in cleanup
    assert "legacy_service_account_token_secret_collection_resource_version" in cleanup
    assert '"delete"' not in cleanup
    assert '"--execute"' not in cleanup


def test_custody_provider_is_distinct_and_legacy_objects_are_adopted_without_delete() -> None:
    providers = _source("stages/foundation/providers.tf")
    custody = _source("stages/pod-security-custody/main.tf")
    custody_variables = _source("stages/pod-security-custody/variables.tf")
    state_handoff = _source("stages/foundation/pod_security_custody_state_handoff.tf")
    manifest_verifier = _source("scripts/verify_sai07_custody_manifest_bundle.py")
    manifest_verifier_v2 = _source("scripts/verify_sai07_custody_manifest_bundle_v2.py")
    trust_verifier = _source("scripts/verify_sai07_custody_trust.py")
    versions = _source("stages/pod-security-custody/versions.tf")
    trust_lock = json.loads(_source("stages/pod-security-custody/custody-trust-lock.json"))

    assert 'alias = "pod_security_custody"' not in providers
    assert "custody_owner_kubeconfig_path" not in providers
    assert 'provider "kubernetes"' in custody
    assert "owner_kubeconfig_path" in custody_variables
    assert "platform_kubeconfig" not in custody_variables
    assert "receipt_kubeconfig" not in custody_variables
    assert "iam_boundary_receipt_path" in custody_variables
    assert "backend_custody_receipt_path" in custody_variables
    assert "owner_username" not in custody_variables
    assert "manifest_public_key_sha256" not in custody_variables
    assert 'backend "s3"' in versions
    assert "use_lockfile = true" in versions
    assert trust_lock["activation"] == "blocked"
    assert "repository-pinned external custody trust is not active" in trust_verifier
    assert 'field_manager {' in custody and "force_conflicts = false" in custody
    assert "prevent_destroy = true" in custody
    assert 'resource "kubernetes_secret_v1" "token_anchor"' in custody
    assert "typed POST create rather than an SSA PATCH" in custody
    assert "kubernetes_secret_v1.token_anchor" in custody
    active_state_handoff = state_handoff.split("/*", 1)[0] + state_handoff.rsplit("*/", 1)[1]
    assert "removed {" not in active_state_handoff
    assert "no state address is relinquished" in state_handoff
    assert "fs2-pod-security-token-anchor" in manifest_verifier
    assert "immutable empty token-anchor Secret" in manifest_verifier
    assert "fs2-pod-security-rollout-custodian" in manifest_verifier
    assert "fs2-pod-security-metadata-reader" in manifest_verifier
    assert "has(object.spec.boundObjectRef)" in manifest_verifier
    assert (
        "a ValidatingAdmissionPolicy cannot be treated as its own custody boundary"
        not in manifest_verifier
    )
    assert "REQUIRED_OBJECTS" in manifest_verifier
    assert "DaemonSet" in manifest_verifier
    assert "STATIC_STATE" in manifest_verifier_v2
    assert "DYNAMIC_ADDRESS_RE" in manifest_verifier_v2
    assert "present_addresses != set(by_address)" in manifest_verifier_v2
    assert "live object changed before SSA" in manifest_verifier_v2
    assert 'metadata["resourceVersion"]' in manifest_verifier_v2


def test_v3_custody_uses_raw_authoritative_evidence_and_retains_platform_state() -> None:
    collector = _source("scripts/collect_sai07_authoritative_custody_evidence.py")
    evidence = _source("scripts/sai07_authoritative_evidence.py")
    trust = _source("scripts/verify_sai07_custody_trust_v3.py")
    manifest = _source("scripts/verify_sai07_custody_manifest_bundle_v3.py")
    pipeline = _source("scripts/run_sai07_retained_state_custody_v3.py")
    executor = _source("scripts/run_sai07_external_execution_v3.py")
    acknowledgement = _source("scripts/verify_sai07_external_execution_ack_v3.py")
    readme = _source("stages/pod-security-custody/README.md")
    lock = json.loads(_source("stages/pod-security-custody/custody-trust-lock-v3.json"))

    assert lock["activation"] == "blocked"
    assert lock["backend_credential_projection"] == {
        "endpoint": None,
        "method": None,
        "response_schema_sha256": None,
        "server_side_fields": [],
        "status": "blocked-no-provider-server-side-metadata-projection",
    }
    assert len(lock["authorities"]) == 3
    assert lock["executor"]["source_path"] == "scripts/run_sai07_external_execution_v3.py"
    assert lock["executor"]["verifier_path"] == "scripts/verify_sai07_external_execution_ack_v3.py"
    assert re.fullmatch(r"[a-f0-9]{64}", lock["executor"]["source_sha256"])
    assert re.fullmatch(r"[a-f0-9]{64}", lock["executor"]["verifier_sha256"])
    assert "provider_receipt" in lock["authorities"]
    assert "backend_receipt" in lock["authorities"]
    assert "manifest" in lock["authorities"]
    assert "ListMembers" in collector and "ListMemberOf" in collector
    assert "AccessPermitService" in collector
    assert "ListAccessKeysByAccountRequest" not in collector
    assert "AccessKeyServiceClient" not in collector
    assert "status.secret" in collector
    assert "server-side access-key metadata projection" in collector
    assert "CLI JSONPath/client filter" in collector
    assert 'calls["service_accounts_by_project"]' in collector
    assert 'calls["federated_credentials_by_project"]' in collector
    assert '"request_id": request_id' in collector and '"trace_id": trace_id' in collector
    assert "get_secret" not in collector.lower()
    assert 'os.O_EXCL' in collector and 'os.fsync' in collector
    assert '"get_bucket_policy"' in collector
    assert '"get_object_lock_configuration"' in collector
    assert 'VersionId=version' in collector
    assert "forward and reverse group membership enumerations differ" in evidence
    assert "does not cover every tenant project" in evidence
    assert "platform authority reaches an external custody resource" in evidence
    assert "protected resource set omits a custody scope or inheritance ancestor" in evidence
    assert "required permit is not bound to its provider identity" in evidence
    assert "backend access group is not the exact singleton collector boundary" in evidence
    assert "backend provider-native policy is not limited to the singleton access group" in evidence
    assert "provider request ID" in evidence and "S3 {field} request ID" in evidence
    assert "platform Terraform state is not version 4" in evidence
    assert "custody_addresses_sha256" in evidence
    assert "provider_evidence_path" in trust and "backend_evidence_path" in trust
    assert "platform_state_path" in trust
    assert "repository-pinned contract path" in trust
    assert "cryptographically distinct" in trust
    assert "independently reconstructed evidence" in trust
    assert "raw backend state addresses" in manifest
    assert '"state_ownership": "platform-retained-no-import-no-forget"' in pipeline
    assert '"custody_field_ownership": "zero-fields-on-platform-state"' in pipeline
    assert "two independently collected generation IDs" in pipeline
    assert '"provider_backend_drift_fenced": "true"' in pipeline
    assert '"terraform",' not in pipeline
    assert "state rm" in pipeline and "terraform import" in pipeline
    assert "--server-side" in executor
    assert "--force-conflicts=false" in executor
    assert 'managed_fields[0].get("fieldsV1") != expected_fields_v1' in executor
    assert "state_omits_ack" in executor
    assert "a Terraform-retained object changed across acknowledgement SSA" in executor
    assert "write_exclusive" in executor
    assert "receipt_consumption_sha256" in executor
    assert "platform_objects_before_sha256" in acknowledgement
    assert "platform_objects_after_sha256" in acknowledgement
    assert "repository-pinned v3 custody activation is blocked" in acknowledgement
    assert "RETAINED REJECTED V2 ROOT" in readme
    assert "SOURCE/INTEGRATION/LIVE NO-GO" in readme


def test_external_custody_pipeline_is_remote_attested_exact_and_non_destructive() -> None:
    pipeline = _source("scripts/run_sai07_external_custody_pipeline.py")
    handoff = _source("scripts/verify_sai07_external_handoff_v2.py")

    assert "verify_sai07_custody_trust.py" in pipeline
    assert "verify_sai07_custody_manifest_bundle_v2.py" in pipeline
    assert "verify_sai07_external_handoff_v2.py" in pipeline
    assert "verify_pod_security_receipts.py" in pipeline
    assert '"delete" in actions' in pipeline
    assert "overwrite is forbidden" in pipeline
    assert "state_lineage" in handoff and "state_serial" in handoff
    assert "pre_objects_sha256" in handoff and "post_objects_sha256" in handoff
    assert "STATIC_STATE" in handoff and "DYNAMIC_ADDRESS_RE" in handoff
    assert "adoption omits a static platform-owned custody address" in handoff


def test_secret_inventory_is_metadata_only_and_bound_to_a_short_lived_actor() -> None:
    collector = _source("scripts/collect_sai07_secret_metadata.py")
    anchor_collector = _source("scripts/collect_sai07_token_anchor_metadata.py")
    baseline = _source("scripts/audit_sai07_baseline_inventory.py")
    cleanup = _source("scripts/cleanup_sai07_legacy_resources.py")

    assert "PartialObjectMetadataList" in collector
    assert '"Accept": MEDIA_TYPE' in collector
    assert "token_bound_object_ref" in collector
    assert 'kubernetes.get("secret")' in collector
    assert "MEDIA_TYPE" in anchor_collector
    assert "token-anchor response contains Secret payload fields" in anchor_collector
    assert 'client.raw("/api/v1/namespaces/fs2-models/secrets")' not in baseline
    assert 'client.raw(f"/api/v1/namespaces/{NAMESPACE}/secrets")' not in cleanup
    assert "--secret-metadata-artifact" in baseline
    assert "--secret-metadata-artifact" in cleanup


def test_effective_authority_audit_covers_cluster_and_every_live_namespace() -> None:
    audit_v1 = _source("scripts/audit_sai07_effective_authority.py")
    audit = _source("scripts/audit_sai07_effective_authority_v2.py")
    handoff = _source("scripts/verify_sai07_external_handoff_v2.py")

    assert 'client.raw("/api/v1/namespaces")' in audit
    assert "for namespace in namespaces" in audit
    assert "self_subject_rules_reviews" in audit
    assert "self_subject_access_reviews" in audit
    assert "expected_reviews" in audit
    for profile in (
        "platform",
        "inactive-owner",
        "token-issuer",
        "receipt-service-account",
        "metadata-reader",
    ):
        assert profile in audit
        assert profile in handoff
    for boundary in (
        "secrets",
        "serviceaccounts",
        "configmaps",
        "daemonsets",
        "deployments",
        "persistentvolumeclaims",
        "persistentvolumes",
        "customresourcedefinitions",
        "certificatesigningrequests",
        "userextras",
        "pods/proxy",
        "services/proxy",
        "rolebindings",
        "clusterrolebindings",
        "validatingadmissionpolicies",
        "validatingadmissionpolicybindings",
        "impersonate",
        "bind",
        "escalate",
    ):
        assert boundary in audit
        assert boundary in handoff

    # Kubernetes' impersonation resource split is security-significant:
    # users/groups/serviceaccounts are core, while uids/userextras are in
    # authentication.k8s.io.  A review against the wrong group is not a denial
    # proof for the real authorization edge.
    impersonation_block = audit.split("IMPERSONATION = (", 1)[1].split(")\n\n", 1)[0]
    assert '("", "users")' in impersonation_block
    assert '("", "groups")' in impersonation_block
    assert '("", "serviceaccounts")' in impersonation_block
    assert '("authentication.k8s.io", "uids")' in impersonation_block
    assert '("authentication.k8s.io", "userextras")' in impersonation_block
    assert '("authentication.k8s.io", "users")' not in impersonation_block
    assert '("authentication.k8s.io", "groups")' not in impersonation_block
    assert '"impersonate:v1/users": ("impersonate", "", "users", "")' in audit_v1
    assert '"impersonate:v1/groups": ("impersonate", "", "groups", "")' in audit_v1
    assert '"impersonate:authentication.k8s.io/uids"' in audit_v1
    assert '"impersonate:authentication.k8s.io/userextras"' in audit_v1
