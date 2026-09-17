"""Unexecuted regressions for the additive SAI-20 v3 authority successor.

The coordinator boundary permits authoring this suite but forbids executing it
in this task.  These assertions encode every blocker from the independent
review of 850c1aeb134196b36250b5e8bd20cf7c1aa1c0aa.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_TF = ROOT / "stages/workloads/sai20_database_authority_v3.tf"
AUTHORITY_PY = ROOT / "stages/workloads/scripts/sai20_database_authority.py"
NETWORK_TF = ROOT / "stages/workloads/sai20_network_isolation.tf"


class Sai20DatabaseAuthorityV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.authority_tf = AUTHORITY_TF.read_text(encoding="utf-8")
        cls.authority_py = AUTHORITY_PY.read_text(encoding="utf-8")
        cls.network_tf = NETWORK_TF.read_text(encoding="utf-8")
        cls.python_tree = ast.parse(cls.authority_py)

    def test_verifier_is_syntactically_parseable_without_executing_it(self) -> None:
        self.assertIsInstance(self.python_tree, ast.Module)

    def test_complete_network_policy_set_is_signed_and_admission_guarded(self) -> None:
        for token in (
            '"network_policy_inventory"',
            '"continue_token"',
            '"remaining_item_count"',
            '"selects_control_database"',
            '"selected_database_pod_uids"',
            '"canonical-exact"',
            '"non-overlapping"',
            '"alternate database ingress policy is present"',
            'selector_explicitly_excludes_database',
        ):
            self.assertIn(token, self.authority_py)
        self.assertIn('resources   = ["networkpolicies"]', self.authority_tf)
        self.assertIn('namespaceSelector', self.authority_tf)
        self.assertIn('"kubernetes.io/metadata.name" = "fs2-data"', self.authority_tf)
        self.assertIn("variables.targetMetadata.name in", self.authority_tf)
        self.assertIn("variables.canonicalSelector || variables.explicitDatabaseExclusion", self.authority_tf)
        self.assertIn("only the canonical policy may select fs2-control-db", self.authority_tf)
        self.assertIn("request.operation != 'DELETE'", self.authority_tf)
        self.assertIn("authority-handoff-sha256", self.authority_tf)

    def test_preexisting_pods_and_every_controller_kind_require_complete_lists(self) -> None:
        resources = {
            "pods",
            "replicationcontrollers",
            "deployments",
            "statefulsets",
            "daemonsets",
            "replicasets",
            "jobs",
            "cronjobs",
        }
        for resource in resources:
            self.assertIn(f'"{resource}"', self.authority_py)
        self.assertIn('seen_lists == RESOURCE_LISTS', self.authority_py)
        self.assertIn('len(objects) == total', self.authority_py)
        self.assertIn('"approved-current"', self.authority_py)
        self.assertIn('"not-a-database-peer"', self.authority_py)
        self.assertIn('"pre-activation"', self.authority_py)
        self.assertIn('"steady-state"', self.authority_py)

    def test_all_retained_storage_generations_have_ingress_and_inventory_disposition(self) -> None:
        for component in (
            "storage-reconciler",
            "storage-reconciler-v2",
            "storage-reconciler-v3",
        ):
            self.assertIn(f'"{component}"', self.authority_py)
            self.assertIn(f'"{component}"', self.authority_tf)
            self.assertIn(f'"{component}"', self.network_tf)
        self.assertIn('sai20_storage_reconciler_v2_component', self.network_tf)
        self.assertIn('sai20_storage_reconciler_v3_component', self.network_tf)
        self.assertGreaterEqual(
            self.network_tf.count('key      = "fs2.nebius.ai/storage-egress-generation"'),
            2,
        )
        self.assertIn('key      = "fs2.nebius.ai/storage-rollout-generation"', self.network_tf)
        self.assertIn('len(generations) == 3', self.authority_py)
        self.assertIn('{"present", "retired"}', self.authority_py)

    def test_handoff_is_ed25519_signed_time_bounded_and_git_object_bound(self) -> None:
        for token in (
            "Ed25519PublicKey",
            "key.verify(signature, canonical(payload))",
            'timedelta(hours=1)',
            'timedelta(minutes=30)',
            'git(repository, "rev-parse", "HEAD")',
            'git(repository, "rev-parse", "HEAD^{tree}")',
            'git(repository, "diff", "--name-only")',
            'git(repository, "diff", "--cached", "--name-only")',
            "REQUIRED_CURRENT_BLOBS",
            "SAI08_BLOBS",
        ):
            self.assertIn(token, self.authority_py)
        for rejected in (
            "cea63190aca6548d8be961a9432cc7cc1277721e",
            "07faac62c6854a7b7947f97f59b5b7b1030813fd",
            "850c1aeb134196b36250b5e8bd20cf7c1aa1c0aa",
        ):
            self.assertIn(rejected, self.authority_py)

    def test_rbac_is_complete_exact_principal_and_non_escalating(self) -> None:
        self.assertIn('seen_lists == RBAC_LISTS', self.authority_py)
        self.assertIn('legacy_group_members"] == []', self.authority_py)
        self.assertIn('impersonation["unaccounted"] == []', self.authority_py)
        self.assertIn('subject["kind"] in {"User", "ServiceAccount"}', self.authority_py)
        self.assertNotIn('kind      = "Group"', self.authority_tf)
        self.assertIn('verbs           = ["get", "update", "patch"]', self.authority_tf)
        self.assertNotRegex(self.authority_tf, r'verbs\s+=\s+\[[^\]]*"delete"')
        self.assertNotRegex(self.authority_tf, r'verbs\s+=\s+\[[^\]]*"create"')
        self.assertIn('resource_names = each.value.names', self.authority_tf)

    def test_broad_legacy_group_cannot_mutate_innocuous_workloads(self) -> None:
        self.assertIn('legacy broad-writer group must be authoritatively empty', self.authority_py)
        self.assertIn('request.resource.resource == %s', self.authority_tf)
        self.assertIn('object.metadata.name in %s', self.authority_tf)
        self.assertIn(
            'variables.releaseMutation || (variables.controllerIdentity && variables.controllerOwnedChild)',
            self.authority_tf,
        )
        self.assertIn('resources   = ["pods", "replicationcontrollers"]', self.authority_tf)
        self.assertIn(
            'resources   = ["deployments", "statefulsets", "daemonsets", "replicasets"]',
            self.authority_tf,
        )
        self.assertIn('resources   = ["jobs", "cronjobs"]', self.authority_tf)

    def test_rejected_format_only_gate_is_cryptographically_bound(self) -> None:
        self.assertIn('legacy_contract_sha256', self.authority_tf)
        self.assertIn('payload["legacy_contract_sha256"] == query["legacy_contract_sha256"]', self.authority_py)
        self.assertIn('terraform_data.sai20_database_authority_v3.output.source_commit', self.authority_tf)
        self.assertIn('terraform_data.sai20_database_authority_v3.output.source_tree', self.authority_tf)


if __name__ == "__main__":
    unittest.main()
