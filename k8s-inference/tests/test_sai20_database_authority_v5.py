"""Unexecuted regressions for the additive SAI-20 v5 successor gate.

The coordinator explicitly forbids executing tests or parsers in this task.
These assertions encode the eight deterministic blockers reported against
efb29e684e0c91b06553d76b43c487a8531016f2. They are authored evidence only;
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
INGRESS = ROOT / "stages/workloads/contracts/sai20-control-db-ingress-v4.json"
BOOTSTRAP = ROOT / "stages/workloads/contracts/sai20-bootstrap-guard-v5.json"
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
        cls.ingress = json.loads(INGRESS.read_text(encoding="utf-8"))
        cls.bootstrap = json.loads(BOOTSTRAP.read_text(encoding="utf-8"))
        cls.roots = json.loads(ROOTS.read_text(encoding="utf-8"))
        cls.anchors = json.loads(ANCHORS.read_text(encoding="utf-8"))
        cls.receipts = json.loads(RECEIPTS.read_text(encoding="utf-8"))

    def test_rejected_v4_source_cannot_be_reused(self) -> None:
        self.assertIn("d5c19b3a8b3345acbec7b16bd5a2c00a455874d8", self.v5_py)
        self.assertIn("efb29e684e0c91b06553d76b43c487a8531016f2", self.v5_py)
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

    def test_preexisting_guard_closes_create_before_binding_window(self) -> None:
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
        self.assertIn("reserved v4/v5 successor admission object already exists", self.v5_py)
        self.assertIn("reobserve_supplemental_kubernetes(query, context)", self.v5_py)
        self.assertIn("v5 apply changed a pre-existing admission object", self.v5_py)
        self.assertIn("v5 apply introduced an unreviewed admission object", self.v5_py)
        self.assertIn("v5 apply did not establish the exact guarded admission-object set", self.v5_py)
        self.assertNotIn('resource "kubernetes_manifest" "sai20_database_authority_bootstrap_guard_v5"', self.v5_tf)

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
        self.assertIn("dangerous or sensitive ClusterRoleBinding authority is not constrained", self.v5_py)
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
        self.assertIn("owner.name.startsWith", self.v5_tf)
        self.assertIn("variables.exactOperatorParent || variables.signedRolloutChild", self.v5_tf)

    def test_authenticator_uid_is_signed_reobserved_and_admitted(self) -> None:
        self.assertIn('uid = text(user_info.get("uid")', self.v4_py)
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

    def test_provider_observer_executes_the_authenticated_open_descriptor(self) -> None:
        self.assertIn("os.O_NOFOLLOW", self.v5_py)
        self.assertIn("os.fstat(observer_fd)", self.v5_py)
        self.assertIn('f"/proc/self/fd/{observer_fd}"', self.v5_py)
        self.assertIn("pass_fds=(observer_fd,)", self.v5_py)
        self.assertIn("provider group observer changed while it was executing", self.v5_py)
        self.assertNotIn("[str(observer)]", self.v5_py)

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


if __name__ == "__main__":
    unittest.main()
