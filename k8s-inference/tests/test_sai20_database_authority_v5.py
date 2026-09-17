"""Unexecuted regressions for the additive SAI-20 v5 successor gate.

The coordinator explicitly forbids executing tests or parsers in this task.
These assertions include the provider-identity, target-classification,
credential-workload and scoped-debug blockers finally reported against
6e1bf0f00d85a80d228a7cea803511391076fb5a, plus the four dynamic
credential-custody blockers finally reported against
e8ac34b7b9dd670015655d43cb24d14907abf8f1, and the four final
ephemeral-debug, projected-token and rollout-identity blockers reported against
1d00f13842ea0287b1aefa628bc0f224c461c65c, plus the exclusive-subresource,
API-injected-token, signer-sign and tenant/model/retention blockers finally
reported against 23aa56e61b5843744636b8112091eaa93ec43407.
They are authored evidence only;
this task's coordinator boundary forbids executing them.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V4_TF = ROOT / "stages/workloads/sai20_database_authority_v4.tf"
V5_TF = ROOT / "stages/workloads/sai20_database_authority_v5.tf"
V4_PY = ROOT / "stages/workloads/scripts/sai20_database_authority_v4.py"
V5_PY = ROOT / "stages/workloads/scripts/sai20_database_authority_v5.py"
PROVIDERS = ROOT / "stages/workloads/providers.tf"
LOCALS = ROOT / "stages/workloads/locals.tf"
VARIABLES = ROOT / "stages/workloads/variables.tf"
FOUNDATION_PROVIDERS = ROOT / "stages/foundation/providers.tf"
FOUNDATION_LOCALS = ROOT / "stages/foundation/locals.tf"
FOUNDATION_RELEASES = ROOT / "stages/foundation/releases.tf"
FOUNDATION_GATE = ROOT / "stages/foundation/kueue_admission_gate.tf"
STACK = ROOT / "inference-stack"
INGRESS = ROOT / "stages/workloads/contracts/sai20-control-db-ingress-v4.json"
BOOTSTRAP = ROOT / "stages/workloads/contracts/sai20-bootstrap-guard-v5.json"
DEBUG_AUTHORIZER = ROOT / "stages/workloads/contracts/sai20-debug-authorizer-v1.json"
DEBUG_RECORD = ROOT / "stages/workloads/contracts/sai20-debug-record-v1.json"
POD_SECRET_REFERENCES = ROOT / "stages/workloads/contracts/sai20-pod-secret-references-v1.json"
ROOTS = ROOT / "security/sai20/authority-roots-v1.json"
ANCHORS = ROOT / "security/sai20/enrollment-authorities-v1.json"
RECEIPTS = ROOT / "security/sai20/root-enrollment-receipts-v1.json"


class Sai20DatabaseAuthorityV5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.v4_tf = V4_TF.read_text(encoding="utf-8")
        cls.v5_tf = V5_TF.read_text(encoding="utf-8")
        cls.v4_py = V4_PY.read_text(encoding="utf-8")
        cls.v5_py = V5_PY.read_text(encoding="utf-8")
        cls.providers = PROVIDERS.read_text(encoding="utf-8")
        cls.locals_tf = LOCALS.read_text(encoding="utf-8")
        cls.variables_tf = VARIABLES.read_text(encoding="utf-8")
        cls.foundation_providers = FOUNDATION_PROVIDERS.read_text(encoding="utf-8")
        cls.foundation_locals = FOUNDATION_LOCALS.read_text(encoding="utf-8")
        cls.foundation_releases = FOUNDATION_RELEASES.read_text(encoding="utf-8")
        cls.foundation_gate = FOUNDATION_GATE.read_text(encoding="utf-8")
        cls.stack = STACK.read_text(encoding="utf-8")
        cls.ingress = json.loads(INGRESS.read_text(encoding="utf-8"))
        cls.bootstrap = json.loads(BOOTSTRAP.read_text(encoding="utf-8"))
        cls.debug_authorizer = json.loads(DEBUG_AUTHORIZER.read_text(encoding="utf-8"))
        cls.debug_record = json.loads(DEBUG_RECORD.read_text(encoding="utf-8"))
        cls.pod_secret_references = json.loads(
            POD_SECRET_REFERENCES.read_text(encoding="utf-8")
        )
        cls.roots = json.loads(ROOTS.read_text(encoding="utf-8"))
        cls.anchors = json.loads(ANCHORS.read_text(encoding="utf-8"))
        cls.receipts = json.loads(RECEIPTS.read_text(encoding="utf-8"))

    def test_rejected_v4_source_cannot_be_reused(self) -> None:
        self.assertIn("d5c19b3a8b3345acbec7b16bd5a2c00a455874d8", self.v5_py)
        self.assertIn("efb29e684e0c91b06553d76b43c487a8531016f2", self.v5_py)
        self.assertIn("a51b1d80738a66774eaef945c6870ba79549a816", self.v5_py)
        self.assertIn("948e1836b4058779aff2c0c91c62aa898968da5d", self.v5_py)
        self.assertIn("17469ed79eb56ae63327f0ddecb81d21b2170722", self.v5_py)
        self.assertIn("6e1bf0f00d85a80d228a7cea803511391076fb5a", self.v5_py)
        self.assertIn("e8ac34b7b9dd670015655d43cb24d14907abf8f1", self.v5_py)
        self.assertIn("1d00f13842ea0287b1aefa628bc0f224c461c65c", self.v5_py)
        self.assertIn("23aa56e61b5843744636b8112091eaa93ec43407", self.v5_py)
        self.assertIn("source is a preserved rejected candidate", self.v5_py)
        self.assertIn("sai20_database_authority_v5_plan.output.successor_verified", self.v4_tf)
        self.assertIn("sai20_database_authority_v5_identity.output.bootstrap_reobserved", self.v4_tf)
        self.assertIn("sai20_database_authority_v5_apply.output.provider_group_reobserved", self.v4_tf)

    def test_cluster_role_bindings_are_derived_not_only_collected(self) -> None:
        self.assertIn('{"rolebindings", "clusterrolebindings"}', self.v5_py)
        self.assertIn('"binding_resource": resource', self.v5_py)
        self.assertIn('item["binding_resource"] == "clusterrolebindings"', self.v5_py)
        self.assertIn("cluster-inclusive dangerous RBAC closure differs", self.v5_py)
        self.assertIn("cluster-inclusive sensitive mutation closure differs", self.v5_py)
        self.assertIn("cluster authority review is not content-derived", self.v5_py)

    def test_database_and_operator_peers_have_complete_exact_owner_inventory(self) -> None:
        for namespace in ("fs2-data", "cnpg-system"):
            self.assertIn(namespace, self.v5_py)
            self.assertIn(namespace, self.v5_tf)
        for resource in (
            "pods",
            "replicationcontrollers",
            "deployments",
            "statefulsets",
            "daemonsets",
            "replicasets",
            "jobs",
            "cronjobs",
        ):
            self.assertIn(f'"{resource}"', self.v5_py)
        self.assertIn("postgresql.cnpg.io/v1", self.v5_py)
        self.assertIn('apiGroups   = ["postgresql.cnpg.io"]', self.v5_tf)
        self.assertIn("variables.cnpgClusterObject", self.v5_tf)
        self.assertIn("database Pod does not have the exact live CNPG Cluster owner", self.v5_py)
        self.assertIn("CNPG operator Pod does not resolve to one exact live operator parent", self.v5_py)
        self.assertIn("CNPG controller username is not authenticator-derived", self.v5_py)
        self.assertIn("sai20_authority_v5_cnpg_controller_identities", self.v5_tf)
        self.assertIn("owner.name == 'fs2-control-db'", self.v5_tf)
        self.assertIn("string(owner.uid)", self.v5_tf)
        self.assertIn("variables.databasePeer", self.v5_tf)
        self.assertIn("variables.exactOperatorParent", self.v5_tf)
        self.assertIn("variables.controllerIdentity", self.v5_tf)

    def test_preexisting_guard_supports_exact_initial_and_renewal_transitions(self) -> None:
        self.assertEqual(
            self.bootstrap["policy"]["name"],
            "fs2-database-authority-bootstrap-guard-v5",
        )
        self.assertEqual(
            self.bootstrap["binding"]["name"],
            "fs2-database-authority-bootstrap-guard-binding-v5",
        )
        self.assertIn("fs2-database-authority-object-custody-v4", self.bootstrap["protected_names"])
        self.assertIn("fs2-database-authority-object-custody-binding-v4", self.bootstrap["protected_names"])
        self.assertEqual(self.bootstrap["binding"]["spec"]["validationActions"], ["Deny"])
        self.assertIn("validatingadmissionpolicies", self.v5_py)
        self.assertIn("validatingadmissionpolicybindings", self.v5_py)
        self.assertIn("pre-existing bootstrap guard policy and binding must both be uniquely active", self.v5_py)
        self.assertIn('mode = "INITIAL"', self.v5_py)
        self.assertIn('mode = "RENEWAL"', self.v5_py)
        self.assertIn('"successor_transition"', self.v5_py)
        self.assertIn("signed successor transition does not bind the exact old admission objects", self.v5_py)
        self.assertIn("reobserve_supplemental_kubernetes(query, context)", self.v5_py)
        self.assertIn("renewal replaced protected admission object", self.v5_py)
        self.assertIn("source-exact successor activation is incomplete", self.v5_py)
        self.assertIn("expected_successor_admission_objects_json", self.v5_tf)
        self.assertIn("successor_source_exact_reobserved", self.v5_tf)
        self.assertIn("successor_activation_sha256", self.v5_tf)
        self.assertNotIn('resource "kubernetes_manifest" "sai20_database_authority_bootstrap_guard_v5"', self.v5_tf)

    def test_secret_reads_and_base_service_account_mutation_are_closed_without_payloads(self) -> None:
        self.assertIn("SECRET_METADATA_ACCEPT", self.v4_py)
        self.assertIn("PartialObjectMetadataList", self.v4_py)
        self.assertIn('set(item) == {"apiVersion", "kind", "metadata"}', self.v4_py)
        self.assertIn("secret_metadata_inventory_sha256", self.v4_tf)
        self.assertIn("secret_metadata_inventory_sha256", self.v5_tf)
        self.assertIn("read-secret/", self.v4_py)
        self.assertIn("secret_resource_names", self.v4_py)
        self.assertIn("mutate-serviceaccounts/", self.v4_py)
        self.assertIn("service_account_resource_names", self.v4_py)
        self.assertIn('"secrets" in resources', self.v5_py)
        self.assertIn('"serviceaccounts" in resources', self.v5_py)
        self.assertIn("metadata-only Secret inventory changed at apply", self.v4_py)

    def test_credential_equivalent_subresources_are_target_classified(self) -> None:
        for resource in (
            "pods/exec",
            "pods/attach",
            "pods/portforward",
            "pods/proxy",
            "pods/ephemeralcontainers",
            "nodes/proxy",
        ):
            self.assertIn(resource, self.v4_py)
        self.assertIn("CREDENTIAL_PIVOT_ACTIONS", self.v4_py)
        self.assertIn("credential_pivot_rule", self.v4_py)
        self.assertIn("pivot_resource_names", self.v4_py)
        self.assertIn("credential-pivot/", self.v4_py)
        self.assertIn('"subresource": subresource', self.v4_py)
        self.assertIn('"name": name', self.v4_py)
        self.assertIn("*v4.CREDENTIAL_PIVOT_RESOURCES", self.v5_py)
        self.assertIn("targeted_pod_pivot(rule, binding_namespace)", self.v5_py)
        self.assertIn(
            "dangerous binding is neither exact-custodian nor an audited TTL debug lease",
            self.v5_py,
        )
        self.assertIn('for principal_id, identity in sorted(context["principal_identities"].items())', self.v4_py)
        self.assertIn("non-custodian gained dangerous authority at apply", self.v4_py)
        self.assertIn(
            '"pods/proxy": ("create", "delete", "get", "patch", "update")',
            self.v4_py,
        )

    def test_actual_terraform_providers_share_the_launchers_sealed_snapshot(self) -> None:
        self.assertEqual(self.providers.count("config_path    = var.provider_kubeconfig_path"), 2)
        self.assertNotIn("config_path    = pathexpand(var.kubeconfig_path)", self.providers)
        self.assertIn('variable "provider_kubeconfig_path"', self.variables_tf)
        self.assertIn('^/proc/[1-9][0-9]*/fd/[0-9]+$', self.variables_tf)
        self.assertIn("filesha256(var.provider_kubeconfig_path)", self.locals_tf)
        self.assertIn('os.memfd_create(\n            "sai20-provider-kubeconfig"', self.stack)
        self.assertIn("with sealed_provider_kubeconfig(run_root) as provider_kubeconfig_path", self.stack)
        self.assertIn("provider_kubeconfig_path=provider_kubeconfig_path", self.stack)
        self.assertIn("provider kubeconfig descriptor is not immutable", self.v4_py)
        self.assertIn("provider kubeconfig is not the source-owned sealed memfd", self.v4_py)
        self.assertIn("sealed_kubeconfig_sha256 == local.provider_kubeconfig_sha256", self.v4_tf)
        self.assertIn("sealed_kubeconfig_sha256 == local.provider_kubeconfig_sha256", self.v5_tf)
        self.assertEqual(
            self.foundation_providers.count(
                "config_path    = var.provider_kubeconfig_path"
            ),
            2,
        )
        self.assertIn("file(var.provider_kubeconfig_path)", self.foundation_locals)
        self.assertIn("kubeconfig_path    = var.provider_kubeconfig_path", self.foundation_releases)
        self.assertIn("FS2_GATE_KUBECONFIG      = var.provider_kubeconfig_path", self.foundation_gate)
        self.assertIn(
            "Keep one exact sealed descriptor alive across both stages",
            self.stack,
        )
        self.assertIn(
            "Downstream planning uses one immutable credential snapshot",
            self.stack,
        )

    def test_credential_targets_and_debug_access_are_scoped_not_blanket_denied(self) -> None:
        for marker in (
            "credential_workload_inventory",
            "protected_service_accounts",
            "protected_secrets",
            "protected_pod_targets",
            "debug_access_leases",
            "DEBUG_LEASE_MAX_SECONDS = 900",
            "generic Pod connect authority over protected targets",
            "without an audited TTL lease",
        ):
            self.assertIn(marker, self.v5_py)
        self.assertIn("scoped_pod_connection_review", self.v4_py)
        self.assertIn("exact-custodian nor an audited TTL debug lease", self.v5_py)
        self.assertIn("fs2-workload-credential-custody-v6", self.v5_tf)
        self.assertIn("fs2-debug-access-custody-v6", self.v5_tf)
        self.assertIn("unchangedCredentialSurface", self.v5_tf)
        self.assertIn("controllerOwnedCredentialChild", self.v5_tf)
        self.assertIn("exactWorkloadWriter", self.v5_tf)
        self.assertIn("exactWorkloadCreate", self.v5_tf)
        self.assertIn("workload_create_contracts", self.v5_py)
        self.assertIn("Pod spec digest differs", self.v5_py)
        self.assertIn("exactDebugRole", self.v5_tf)
        self.assertIn("exactDebugBinding", self.v5_tf)
        self.assertIn("exactSignedDebugLease", self.v5_tf)
        self.assertIn("debug_access_bindings_json", self.v5_py)
        self.assertIn("duration('900s')", self.v5_tf)
        self.assertIn("debug-audit-id", self.v5_tf)
        self.assertIn("debug-tenant-id", self.v5_tf)
        self.assertIn("debug-reason-sha256", self.v5_tf)
        self.assertIn("fs2-debug-request-authorizer-v6", self.v5_tf)
        self.assertIn('failurePolicy           = "Fail"', self.v5_tf)
        self.assertIn('operations  = ["CONNECT"]', self.v5_tf)
        self.assertIn("debug authorizer attestation does not bind", self.v5_py)
        decision = self.debug_authorizer["decision_contract"]
        self.assertEqual(decision["maximum_lease_seconds"], 900)
        self.assertEqual(decision["maximum_activation_seconds"], 604800)
        self.assertEqual(decision["maximum_clock_skew_seconds"], 5)
        self.assertEqual(decision["stale_or_replayed_lease"], "deny")
        self.assertEqual(decision["unavailable_or_invalid_evidence"], "deny")

    def test_static_and_admission_classifiers_share_every_pod_secret_reference(self) -> None:
        references = self.pod_secret_references["references"]
        self.assertEqual(
            [reference["id"] for reference in references],
            sorted(reference["id"] for reference in references),
        )
        self.assertEqual(
            {reference["id"] for reference in references},
            {
                "azure-file",
                "cephfs",
                "cinder",
                "container-env",
                "container-env-from",
                "csi-node-publish",
                "ephemeral-container-env",
                "ephemeral-container-env-from",
                "flex-volume",
                "image-pull",
                "init-container-env",
                "init-container-env-from",
                "iscsi",
                "projected-volume",
                "rbd",
                "scale-io",
                "secret-volume",
                "storage-os",
            },
        )
        paths = {tuple(reference["path"]) for reference in references}
        for path in (
            ("volumes", "*", "azureFile", "secretName"),
            ("volumes", "*", "cephfs", "secretRef", "name"),
            ("volumes", "*", "cinder", "secretRef", "name"),
            ("volumes", "*", "csi", "nodePublishSecretRef", "name"),
            ("volumes", "*", "flexVolume", "secretRef", "name"),
            ("volumes", "*", "iscsi", "secretRef", "name"),
            ("volumes", "*", "rbd", "secretRef", "name"),
            ("volumes", "*", "scaleIO", "secretRef", "name"),
            ("volumes", "*", "storageos", "secretRef", "name"),
        ):
            self.assertIn(path, paths)
        self.assertIn("POD_SECRET_REFERENCE_PATHS", self.v5_py)
        self.assertIn("pod_secret_reference_surface", self.v5_py)
        self.assertIn("bool(secret_names)", self.v5_py)
        self.assertIn("sai20_authority_v5_pod_secret_reference_contract", self.v5_tf)
        self.assertIn("targetSecretReferenceSurface", self.v5_tf)
        self.assertIn("targetHasSecretReference", self.v5_tf)
        self.assertIn("targetAutomountServiceAccountToken", self.v5_tf)
        self.assertIn('"automount_service_account_token"', self.v5_py)
        self.assertNotIn("namespaceProtectedSecrets", self.v5_tf)
        self.assertNotIn("targetProtectedSecrets", self.v5_tf)

    def test_debug_authorizer_denies_unknown_and_replacement_pod_uids(self) -> None:
        decision = self.debug_authorizer["decision_contract"]
        self.assertEqual(
            decision["target_inventory"],
            "require_exact_signed_namespace_pod_name_uid_and_credential_classification",
        )
        self.assertEqual(
            decision["unknown_or_replacement_uid"],
            "deny_generation_refresh_required",
        )
        self.assertIn("debug_targets", self.v5_py)
        self.assertIn("debug_target_inventory_sha256", self.v5_py)
        self.assertIn("credential_boundary_sha256", self.v5_py)
        self.assertIn("debug-target-inventory-sha256", self.v5_tf)
        self.assertIn("credential-boundary-sha256", self.v5_tf)

    def test_ephemeral_container_updates_cross_both_debug_and_credential_custody(self) -> None:
        self.assertGreaterEqual(
            self.v5_tf.count('resources   = ["pods/ephemeralcontainers"]'),
            2,
        )
        self.assertIn("request.subResource == 'ephemeralcontainers' ? object.spec", self.v5_tf)
        self.assertIn("request.subResource == 'ephemeralcontainers' ? oldObject.spec", self.v5_tf)
        self.assertIn("sai20_authority_v5_exact_debug_ephemeral_update_terms", self.v5_tf)
        self.assertIn("exactDebugEphemeralUpdate", self.v5_tf)
        self.assertIn("debugCredentialSurfacePreserved", self.v5_tf)
        self.assertIn("request.subResource == 'ephemeralcontainers' ?", self.v5_tf)
        self.assertIn("request.subResource == ''", self.v5_tf)
        self.assertNotIn("!variables.credentialBearing || variables.custodian", self.v5_tf)
        self.assertIn("including an otherwise unprotected target or custodian caller", self.v5_tf)
        self.assertIn("sai20_authority_v5_unchanged_secret_references_cel", self.v5_tf)
        self.assertIn("lease.pod_uid", self.v5_tf)
        self.assertIn("variables.targetObject.metadata.uid", self.v5_tf)
        self.assertIn(
            "an exact leased ephemeral-container UPDATE with no credential change",
            self.v5_tf,
        )
        self.assertIn(
            "ephemeral_container_update_intersects_exact_lease_and_credential_custody",
            self.debug_authorizer["decision_contract"]["credential_boundary"],
        )
        self.assertEqual(
            self.debug_authorizer["decision_contract"][
                "ephemeral_container_admission_operation"
            ],
            "UPDATE_for_signed_patch_or_update_lease",
        )

    def test_privileged_controller_create_is_exact_inert_and_two_phase(self) -> None:
        self.assertIn('"object_spec", "object_spec_sha256", "pod_spec"', self.v5_py)
        self.assertIn("controller must be created with replicas zero", self.v5_py)
        self.assertIn("batch controller must be created suspended", self.v5_py)
        self.assertIn("contradictory source-owned node affinity", self.v5_py)
        self.assertIn("variables.targetObject.spec ==", self.v5_tf)
        self.assertIn("local.sai20_authority_v5_protected_workload_objects", self.v5_tf)
        self.assertIn("string(variables.targetObject.metadata.uid)", self.v5_tf)
        self.assertNotIn('parent.resource == "deployments" ?', self.v5_tf)
        self.assertNotIn('parent.resource == "cronjobs" ?', self.v5_tf)

    def test_kubectl_is_executed_only_from_a_sealed_static_elf_snapshot(self) -> None:
        self.assertIn('os.memfd_create("sai20-kubectl"', self.v4_py)
        self.assertIn("before.st_uid == 0", self.v4_py)
        self.assertIn("before.st_mode & 0o022 == 0", self.v4_py)
        self.assertIn("fcntl.F_SEAL_WRITE", self.v4_py)
        self.assertIn("kubectl must be a native ELF executable", self.v4_py)
        self.assertIn("kubectl must be a static ELF with no external interpreter", self.v4_py)
        self.assertIn('f"/proc/self/fd/{descriptor}"', self.v4_py)
        self.assertIn("pass_fds=(descriptor, kubeconfig_descriptor)", self.v4_py)
        self.assertNotIn("[str(binary)", self.v4_py)

    def test_kubeconfig_and_credential_transport_are_sealed_and_self_contained(self) -> None:
        self.assertIn('os.memfd_create("sai20-kubeconfig"', self.v4_py)
        self.assertIn("validate_self_contained_kubeconfig", self.v4_py)
        self.assertIn("selected kubeconfig cluster must use only an inline CA and direct server", self.v4_py)
        self.assertIn("selected kubeconfig user must use only an inline token or inline client certificate/key", self.v4_py)
        self.assertIn('"--kubeconfig", f"/proc/self/fd/{kubeconfig_descriptor}"', self.v4_py)
        self.assertIn("pass_fds=(descriptor, kubeconfig_descriptor)", self.v4_py)
        self.assertIn('env={"HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"}', self.v4_py)
        self.assertIn("sealed_kubeconfig_sha256", self.v4_tf)
        self.assertIn("sealed_kubeconfig_sha256", self.v5_tf)
        self.assertNotIn('"--kubeconfig", query["kubeconfig_path"]', self.v4_py)

    def test_successor_activation_is_bound_to_exact_source_rendered_objects(self) -> None:
        for resource in (
            "sai20_database_authority_object_custody_v4.manifest",
            "sai20_database_authority_object_custody_binding_v4.manifest",
            "sai20_database_policy_freeze_v4.manifest",
            "sai20_database_policy_freeze_binding_v4.manifest",
            "sai20_database_exact_owner_v4.manifest",
            "sai20_database_exact_owner_binding_v4.manifest",
            "sai20_database_peer_identity_v5.manifest",
            "sai20_database_peer_identity_binding_v5.manifest",
            "sai20_workload_credential_custody_v6.manifest",
            "sai20_workload_credential_custody_binding_v6.manifest",
            "sai20_debug_access_custody_v6.manifest",
            "sai20_debug_access_custody_binding_v6.manifest",
            "sai20_cluster_rbac_custody_v6.manifest",
            "sai20_cluster_rbac_custody_binding_v6.manifest",
            "sai20_debug_request_authorizer_v6.manifest",
        ):
            self.assertIn(resource, self.v5_tf)
        self.assertIn("source-rendered successor admission object set differs", self.v5_py)
        self.assertIn("source-rendered successor admission digest differs from the signed transition", self.v5_py)
        self.assertIn("v5 apply successor object is not source-exact", self.v5_py)
        self.assertIn("live successor admission digest differs from signed source render", self.v5_py)

    def test_cluster_scoped_rbac_cannot_bypass_target_aware_debug_custody(self) -> None:
        self.assertIn("fs2-cluster-rbac-custody-v6", self.v5_tf)
        self.assertIn('resources   = ["clusterroles", "clusterrolebindings"]', self.v5_tf)
        self.assertIn(
            "cluster-scoped RBAC mutation requires the freshly re-observed exact custodian",
            self.v5_tf,
        )
        bootstrap_rules = self.bootstrap["policy"]["spec"]["matchConstraints"][
            "resourceRules"
        ]
        self.assertTrue(
            any(
                rule["resources"] == ["clusterroles", "clusterrolebindings"]
                and rule["operations"] == ["CREATE", "UPDATE", "DELETE"]
                for rule in bootstrap_rules
            )
        )
        self.assertTrue(
            any(
                variable["name"] == "clusterRbacObject"
                for variable in self.bootstrap["policy"]["spec"]["variables"]
            )
        )

    def test_root_enrollment_requires_preexisting_external_signature(self) -> None:
        self.assertEqual(self.roots["status"], "ENROLLMENT_REQUIRED")
        self.assertEqual(self.anchors["status"], "ENROLLMENT_REQUIRED")
        self.assertEqual(self.anchors["authorities"], [])
        self.assertEqual(self.receipts["status"], "ENROLLMENT_REQUIRED")
        self.assertEqual(self.receipts["receipts"], [])
        self.assertIn("anchor_commit != root_commit", self.v5_py)
        self.assertIn("authority snapshot is not a strict ancestor", self.v5_py)
        self.assertIn("external enrollment signature invalid", self.v5_py)
        self.assertIn("platform-security-enrollment-authority", self.v5_py)
        self.assertIn("external enrollment receipt closure differs from evidence roots", self.v5_py)

    def test_provider_group_membership_is_reobserved_during_apply(self) -> None:
        self.assertIn("reobserve_provider_group(query, context)", self.v5_py)
        self.assertIn("provider_group_observer_path", self.v5_tf)
        self.assertIn("provider observer differs from the signed executable", self.v5_py)
        self.assertIn("provider group membership changed between signed observation and apply", self.v5_py)
        self.assertIn('result.provider_group_reobserved == "true"', self.v5_tf)
        self.assertEqual(self.v5_tf.count("nonce                   = timestamp()"), 2)

    def test_singleton_cnpg_reread_does_not_require_list_items(self) -> None:
        singleton_branch = self.v5_py.index('if name == "k8s/cnpg-cluster/fs2-data/fs2-control-db"')
        list_requirement = self.v5_py.index('isinstance(current.get("items"), list)', singleton_branch)
        self.assertLess(singleton_branch, list_requirement)
        self.assertIn("CNPG Cluster singleton differs from the signed observation", self.v5_py)

    def test_cluster_and_cnpg_namespace_rbac_are_closed_over_exact_principals(self) -> None:
        self.assertIn('(\"cnpg-system\", \"roles\")', self.v4_py)
        self.assertIn('(\"cnpg-system\", \"rolebindings\")', self.v4_py)
        self.assertIn('record["binding_resource"] == "clusterrolebindings"', self.v5_py)
        self.assertIn(
            "dangerous binding is neither exact-custodian nor an audited TTL debug lease",
            self.v5_py,
        )
        self.assertIn("sensitive RoleBinding/ClusterRoleBinding authority is not constrained to an exact admitted principal", self.v5_py)
        self.assertIn("admitted_subjects", self.v5_py)

    def test_update_cannot_strip_a_protected_peer_label(self) -> None:
        self.assertIn('name = "oldEffectivePodLabels"', self.v5_tf)
        self.assertIn('name = "oldDatabasePeer"', self.v5_tf)
        self.assertIn('name = "oldOperatorPeer"', self.v5_tf)
        self.assertIn("removing a protected database or CNPG peer label requires", self.v5_tf)

    def test_cnpg_rollout_uses_signed_deployment_lineage(self) -> None:
        self.assertIn('ROLLOUT_LINEAGE_LABEL = "security.fs2.nebius.ai/sai20-rollout-lineage"', self.v5_py)
        self.assertIn("CNPG ReplicaSet does not resolve to one exact signed Deployment root", self.v5_py)
        self.assertIn("rollout_lineages_json", self.v5_py)
        self.assertIn("sai20_authority_v5_rollout_child_cel", self.v5_tf)
        self.assertNotIn("owner.name.startsWith", self.v5_tf)
        self.assertNotIn("signedRolloutChild", self.v5_tf)
        self.assertIn('request.resource.resource == \'replicasets\'', self.v5_tf)
        self.assertIn("variables.targetObject.spec.replicas == 0", self.v5_tf)
        self.assertIn("request.userInfo.username", self.v5_tf)
        self.assertIn('"controller_username"', self.v5_py)
        self.assertIn("exactSignedOperatorObject", self.v5_tf)
        self.assertIn("request.operation != 'CREATE'", self.v5_tf)

    def test_authenticator_uid_is_signed_reobserved_and_admitted(self) -> None:
        self.assertIn('uid = user_info.get("uid", "")', self.v4_py)
        self.assertIn("allow_empty_uid=principal[\"class\"] == \"controller\"", self.v4_py)
        self.assertIn('"executor_uid": executor["uid"]', self.v4_py)
        self.assertIn('live_identity["uid"] == context["executor"]["uid"]', self.v4_py)
        self.assertIn('"principal_uids"', self.v5_py)
        self.assertIn("has(request.userInfo.uid)", self.v4_tf)
        self.assertIn("has(request.userInfo.uid)", self.v5_tf)
        self.assertIn("${executor_uid_json}", json.dumps(self.bootstrap))

    def test_sensitive_authority_includes_token_csr_and_impersonation_edges(self) -> None:
        for resource in (
            "serviceaccounts/token",
            "certificatesigningrequests",
            "certificatesigningrequests/approval",
            "signers",
            "uids",
            "userextras",
        ):
            self.assertIn(resource, self.v4_py)
            self.assertIn(resource, self.v5_py)
        self.assertIn('"sign-certificate-signers"', self.v4_py)
        self.assertIn('{"sign", "*"}', self.v4_py)
        self.assertIn('{"sign", "*"}', self.v5_py)
        self.assertIn("sign-certificate-signer/", self.v4_py)
        self.assertIn("signer_resource_names", self.v4_py)

    def test_debug_activation_is_exact_tenant_model_and_retained_ninety_days(self) -> None:
        self.assertIn('"customer_request_sha256"', self.v5_py)
        self.assertIn('"model_id"', self.v5_py)
        self.assertIn("exact signed tenant/model activation request", self.v5_py)
        self.assertIn("debug-model-id", self.v5_tf)
        self.assertIn("debug-customer-request-sha256", self.v5_tf)
        self.assertEqual(self.debug_record["retention_seconds"], 7776000)
        self.assertEqual(
            self.debug_record["commit_order"],
            "durable_record_commit_before_admission_response",
        )
        self.assertTrue(
            {"tenant_id", "model_id", "customer_request_sha256"}
            <= set(self.debug_record["required_fields"])
        )
        self.assertEqual(
            self.debug_authorizer["decision_contract"]["decision_record"],
            "durably_append_allow_or_deny_before_response_and_retain_exactly_7776000_seconds",
        )

    def test_provider_observer_executes_the_authenticated_open_descriptor(self) -> None:
        self.assertIn("os.O_NOFOLLOW", self.v5_py)
        self.assertIn("os.fstat(observer_fd)", self.v5_py)
        self.assertIn("before.st_uid == 0", self.v5_py)
        self.assertIn("before.st_mode & 0o022 == 0", self.v5_py)
        self.assertIn("os.memfd_create", self.v5_py)
        self.assertIn("fcntl.F_ADD_SEALS", self.v5_py)
        self.assertIn("fcntl.F_SEAL_WRITE", self.v5_py)
        self.assertIn('f"/proc/self/fd/{sealed_fd}"', self.v5_py)
        self.assertIn("pass_fds=(sealed_fd,)", self.v5_py)
        self.assertIn("provider group observer changed while it was executing", self.v5_py)
        self.assertIn('v4.require_static_elf(sealed_fd, before.st_size, "provider group observer")', self.v5_py)
        self.assertIn('env={"HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"}', self.v5_py)
        self.assertNotIn("[str(observer)]", self.v5_py)

    def test_authority_closure_is_cluster_wide_name_exact_and_fresh_for_every_principal(self) -> None:
        self.assertIn('NAMESPACE_ENDPOINT = "/api/v1/namespaces"', self.v4_py)
        self.assertIn("service_account_endpoints(namespaces)", self.v4_py)
        self.assertIn('f"k8s/rbac/{namespace or \'_cluster\'}/{resource}"', self.v4_py)
        self.assertIn('"subresource": "token"', self.v4_py)
        self.assertIn('"name": account["name"]', self.v4_py)
        self.assertIn("impersonate-exact-custodian-user", self.v4_py)
        self.assertIn("impersonate-exact-custodian-uid", self.v4_py)
        self.assertIn("impersonate-exact-custodian-group", self.v4_py)
        self.assertIn("impersonate-exact-custodian-extra", self.v4_py)
        self.assertIn("SUBJECT_ACCESS_REVIEW_ENDPOINT", self.v4_py)
        self.assertIn('for principal_id, identity in sorted(context["principal_identities"].items())', self.v4_py)
        self.assertIn("non-custodian gained dangerous authority at apply", self.v4_py)
        self.assertIn("service_account_inventory_sha256", self.v4_tf)
        self.assertIn("service_account_inventory_sha256", self.v5_tf)

    def test_existing_database_clients_and_debugging_paths_remain_present(self) -> None:
        serialized = json.dumps(self.ingress, sort_keys=True)
        for component in (
            "storage-reconciler",
            "storage-reconciler-v2",
            "storage-reconciler-v3",
            "grafana",
            "prometheus",
            "acceptance",
            "cloudnative-pg",
        ):
            self.assertIn(component, serialized)
        self.assertNotIn("request-debug", self.v5_tf)
        self.assertNotIn("customer-storage", self.v5_tf)

    def test_ephemeral_debugger_is_exact_signed_and_cannot_mount_pod_volumes(self) -> None:
        self.assertIn("verify_debug_ephemeral_container", self.v5_py)
        self.assertIn('"ephemeral_container_sha256"', self.v5_py)
        self.assertIn('"volumeMounts" not in value', self.v5_py)
        self.assertIn('"volumeDevices" not in value', self.v5_py)
        self.assertNotIn('"targetContainerName",', self.v5_py)
        self.assertIn('targets[target_key]["ephemeral_debug_safe"]', self.v5_py)
        for field in ("hostIPC", "hostNetwork", "hostPID", "shareProcessNamespace"):
            self.assertIn(field, self.v5_py)
        self.assertIn("capabilities must drop ALL", self.v5_py)
        self.assertIn("RuntimeDefault", self.v5_py)
        self.assertIn(
            "variables.targetEphemeralContainers == variables.oldEphemeralContainers +",
            self.v5_tf,
        )
        decision = self.debug_authorizer["decision_contract"]
        self.assertEqual(
            decision["ephemeral_container_spec"],
            "require_exact_dual_signed_full_spec_and_sha256",
        )
        self.assertIn("forbid_all_volumeMounts", decision["ephemeral_container_storage"])
        self.assertIn(
            "forbid_targetContainerName",
            decision["ephemeral_container_namespace_isolation"],
        )

    def test_explicit_projected_service_account_tokens_are_credential_bearing(self) -> None:
        projection = self.pod_secret_references["service_account_token_projection"]
        self.assertEqual(projection["id"], "projected-service-account-token")
        self.assertEqual(
            projection["path"],
            ["volumes", "*", "projected", "sources", "*", "serviceAccountToken"],
        )
        for field in ("audience=", "expiration_seconds=", "path="):
            self.assertIn(field, projection["cel_surface"])
        self.assertIn("pod_service_account_token_projection_surface", self.v5_py)
        self.assertIn("or has_token_projection", self.v5_py)
        self.assertIn("targetHasServiceAccountTokenProjection", self.v5_tf)
        self.assertIn("targetServiceAccountTokenProjectionSurface", self.v5_tf)
        self.assertEqual(
            projection["default_admission_volume_name_pattern"],
            "kube-api-access-[a-z0-9]{5}",
        )
        self.assertIn("service_account_admission_projection", self.v5_py)
        self.assertIn("service_account_admission_profiles", self.v5_py)
        self.assertIn("service_account_admission_profiles_json", self.v5_tf)
        self.assertIn("targetServiceAccountAdmissionProjection", self.v5_tf)
        self.assertIn('"automount_service_account_token"', self.v4_py)
        self.assertIn("authenticated Pods do not expose one cluster-wide", self.v5_py)
        self.assertIn("filter(group, group.size() > 0)", projection["cel_surface"])
        self.assertIn(
            "direct signed Pod must disable admission-time token injection",
            self.v5_py,
        )

    def test_native_controller_identity_is_live_observed_not_hard_coded(self) -> None:
        self.assertIn("verify_workload_controller_transitions", self.v5_py)
        self.assertIn('authorization["workload_controller_transitions"]', self.v5_py)
        self.assertIn('v4_context["principal_identities"][principal_id]', self.v5_py)
        self.assertIn("workload_controller_transitions_json", self.v5_tf)
        self.assertIn('principal.uid == "" ? "!has(request.userInfo.uid)"', self.v5_tf)
        self.assertNotIn("NATIVE_WORKLOAD_CONTROLLER_IDENTITY", self.v5_py)
        self.assertNotIn('"system:kube-controller-manager"', self.v5_py)

    def test_deployment_and_cronjob_rollouts_use_authenticated_multi_hop_lineage(self) -> None:
        self.assertIn("CREDENTIAL_ROLLOUT_LINEAGE_LABEL", self.v5_py)
        self.assertIn('"deployments", "cronjobs"', self.v5_py)
        self.assertIn('"replicasets", "jobs"', self.v5_py)
        self.assertIn("credential rollout intermediate does not preserve", self.v5_py)
        self.assertIn("credential rollout Pod does not preserve", self.v5_py)
        self.assertIn("credential_rollout_lineages_json", self.v5_tf)
        self.assertIn("sai20_authority_v5_multi_hop_credential_child_terms", self.v5_tf)
        self.assertIn("second_parent_api_version", self.v5_tf)
        self.assertIn("second_controller_identity", self.v5_tf)
        self.assertIn("multiHopCredentialChild", self.v5_tf)


if __name__ == "__main__":
    unittest.main()
