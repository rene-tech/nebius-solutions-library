"""Unexecuted regressions for the additive SAI-20 v4 authority successor.

The coordinator boundary permits authoring these static assertions but forbids
executing them in this task. They encode every deterministic blocker from the
independent review of ffd8674063314f876b1e0b00b73a76fdb4ea27af.
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_TF = ROOT / "stages/workloads/sai20_database_authority_v4.tf"
AUTHORITY_V3_TF = ROOT / "stages/workloads/sai20_database_authority_v3.tf"
AUTHORITY_PY = ROOT / "stages/workloads/scripts/sai20_database_authority_v4.py"
NETWORK_TF = ROOT / "stages/workloads/sai20_network_isolation.tf"
ROOT_REGISTRY = ROOT / "security/sai20/authority-roots-v1.json"
INGRESS_CONTRACT = ROOT / "stages/workloads/contracts/sai20-control-db-ingress-v4.json"


class Sai20DatabaseAuthorityV4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.authority_tf = AUTHORITY_TF.read_text(encoding="utf-8")
        cls.authority_v3_tf = AUTHORITY_V3_TF.read_text(encoding="utf-8")
        cls.authority_py = AUTHORITY_PY.read_text(encoding="utf-8")
        cls.network_tf = NETWORK_TF.read_text(encoding="utf-8")
        cls.registry = json.loads(ROOT_REGISTRY.read_text(encoding="utf-8"))
        cls.ingress = json.loads(INGRESS_CONTRACT.read_text(encoding="utf-8"))
        cls.python_tree = ast.parse(cls.authority_py)

    def test_verifier_is_syntactically_parseable_without_executing_it(self) -> None:
        self.assertIsInstance(self.python_tree, ast.Module)

    def test_authority_roots_are_source_owned_and_fail_closed_before_enrollment(self) -> None:
        self.assertEqual(self.registry["status"], "ENROLLMENT_REQUIRED")
        self.assertEqual(self.registry["roots"], [])
        self.assertEqual(
            set(self.registry["required_roles"]),
            {"evidence-collector", "independent-reviewer"},
        )
        self.assertIn("root registry is not ACTIVE", self.authority_py)
        self.assertIn("minimum_distinct_signatures", self.authority_py)
        self.assertIn("provenance document does not enroll this exact root", self.authority_py)
        self.assertIn("independent review receipt is not content-derived", self.authority_py)
        self.assertNotIn("public_key_path", self.authority_tf)
        self.assertNotIn("public_key_sha256", self.authority_tf)
        self.assertNotIn("authority_key_id", self.authority_tf)

    def test_raw_api_transcripts_are_exact_complete_and_credential_bound(self) -> None:
        for token in (
            '"body_base64"',
            '"body_sha256"',
            '"credential_subject_sha256"',
            '"audit_id"',
            '"request_id"',
            "expected_transcript_names",
            "transcript does not have the exact source-defined request closure",
            "receipt differs from raw API response",
            "effective permissions digest is opaque",
            "legacy group membership request is not authoritative or complete",
            "dangerous RBAC subject closure differs",
            "sensitive mutation subject closure differs",
            "v4 RBAC inventory digest is not content-derived",
            "v4 effective permissions digest is not content-derived",
            "v4 impersonation receipt digest is not content-derived",
            "provider group receipt is not content-derived",
        ):
            self.assertIn(token, self.authority_py)
        for resource in (
            "pods",
            "replicationcontrollers",
            "deployments",
            "statefulsets",
            "daemonsets",
            "replicasets",
            "jobs",
            "cronjobs",
            "roles",
            "rolebindings",
            "clusterroles",
            "clusterrolebindings",
        ):
            self.assertIn(f'"{resource}"', self.authority_py)
        self.assertIn('("fs2-data", "roles")', self.authority_py)
        self.assertIn('("fs2-data", "rolebindings")', self.authority_py)
        self.assertIn("k8s/collector/selfsubjectreview", self.authority_py)

    def test_ingress_content_is_source_owned_recomputed_and_admission_enforced(self) -> None:
        self.assertEqual(self.ingress["podSelector"]["matchLabels"], {"cnpg.io/cluster": "fs2-control-db"})
        self.assertEqual(self.ingress["policyTypes"], ["Ingress"])
        self.assertEqual(len(self.ingress["ingress"]), 9)
        for component in ("storage-reconciler", "storage-reconciler-v2", "storage-reconciler-v3"):
            self.assertIn(component, INGRESS_CONTRACT.read_text(encoding="utf-8"))
        self.assertIn("ingress_sha = digest(ingress_contract)", self.authority_py)
        self.assertIn("planned policy digest is not source-derived", self.authority_py)
        self.assertIn("object.spec == ${jsonencode(local.sai20_authority_v4_ingress_contract)}", self.authority_tf)
        self.assertIn("kubernetes_config_map_v1", self.authority_tf)
        self.assertIn("immutable = true", self.authority_tf)
        self.assertIn("ingress-spec-sha256", self.network_tf)

    def test_apply_reobservation_prevents_saved_plan_replay(self) -> None:
        self.assertEqual(self.authority_tf.count("nonce         = timestamp()"), 2)
        self.assertIn('mode            = "identity"', self.authority_tf)
        self.assertIn('mode            = "apply"', self.authority_tf)
        self.assertIn("identity_reobserved", self.authority_tf)
        self.assertIn("apply_reobserved", self.authority_tf)
        self.assertIn("verify_apply(query, context, roots)", self.authority_py)
        self.assertIn("v4 bundle is expired or not yet valid", self.authority_py)
        self.assertIn("apply-time re-observation differs", self.authority_py)
        self.assertIn('"SelfSubjectRulesReview"', self.authority_py)
        self.assertIn('"SelfSubjectAccessReview"', self.authority_py)
        self.assertIn("sai20_database_policy_freeze_binding_v4", self.authority_tf)
        self.assertIn("exact source-rooted NetworkPolicy specs", self.authority_tf)

    def test_executor_is_the_signed_custodian_before_bootstrap(self) -> None:
        self.assertIn("Terraform executor is not the signed custodian", self.authority_py)
        self.assertIn("executor_principal_id == local.sai20_authority_v3_custodian.id", self.authority_v3_tf)
        self.assertIn("executor_username == local.sai20_authority_v3_custodian.username", self.authority_v3_tf)
        self.assertIn("sai20_database_authority_object_custody_v4", self.authority_tf)
        self.assertIn("depends_on = [terraform_data.sai20_database_authority_v4_identity]", self.authority_tf)
        self.assertIn("resources   = [\"roles\", \"rolebindings\"]", self.authority_tf)
        self.assertIn("request.userInfo.extra ==", self.authority_tf)

    def test_controller_children_require_exact_live_parent_identity(self) -> None:
        for field in ("api_version", "kind", "name", "uid", "controller_username"):
            self.assertIn(f'"{field}"', self.authority_py)
        self.assertIn("authorized parent list is not the exact live database-controller closure", self.authority_py)
        self.assertIn("owner.apiVersion == %s", self.authority_tf)
        self.assertIn("owner.kind == %s", self.authority_tf)
        self.assertIn("owner.name == %s", self.authority_tf)
        self.assertIn("string(owner.uid) == %s", self.authority_tf)
        self.assertIn("request.userInfo.username == %s", self.authority_tf)
        self.assertIn("variables.exactLiveParent", self.authority_tf)

    def test_rejected_candidate_and_zero_delete_rollout_contract_are_preserved(self) -> None:
        self.assertIn("ffd8674063314f876b1e0b00b73a76fdb4ea27af", self.authority_py)
        self.assertIn("request.operation != 'DELETE'", self.authority_tf)
        self.assertIn("storage-reconciler-v2", self.network_tf)
        self.assertIn("storage-reconciler-v3", self.network_tf)


if __name__ == "__main__":
    unittest.main()
