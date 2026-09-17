"""Unexecuted static regressions for the SAI-20 custody successor.

The coordinator authoring boundary forbids executing this module in this task.
It binds the SAI-20 policy to the exact SAI-08 client provenance and rejects
the two label-custody bypass classes found during independent review.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NETWORK = ROOT / "stages/workloads/sai20_network_isolation.tf"
CUSTODY = ROOT / "stages/workloads/sai20_database_custody.tf"
CONTRACT = ROOT / "tests/fixtures/sai20/sai08-storage-reconciler-v3-contract.json"


class Sai20CustodySuccessorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.network = NETWORK.read_text(encoding="utf-8")
        cls.custody = CUSTODY.read_text(encoding="utf-8")
        cls.contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    def test_exact_sai08_database_client_is_admitted_with_both_generations(self) -> None:
        source = self.contract["source"]
        client = self.contract["database_client"]
        self.assertEqual(source["commit"], "6eb13e345c8b17420d1217a70d83e4974497b2b0")
        self.assertEqual(source["tree"], "bd55519891c3f653f11465bf59e997170ff8bf4a")
        self.assertEqual(
            source["reconciler_template"],
            {
                "path": "k8s-inference/charts/security/customer-storage-reconciler-v2/templates/reconciler.yaml",
                "blob": "01966d2e3cc3caa72e70c7b0b96737fd0e5beac7",
            },
        )
        self.assertEqual(
            source["egress_boundary"],
            {
                "path": "k8s-inference/security/customer-storage-egress-boundary/main.tf",
                "blob": "530c3a6ad53a871238aa1819ffa06f585aeaf86f",
            },
        )
        self.assertEqual(client["component"], "storage-reconciler-v3")
        self.assertEqual(client["database_environment_variable"], "FS2_DATABASE_URL")
        self.assertEqual(client["egress_destination_namespace"], "fs2-data")
        self.assertEqual(client["egress_destination_cluster"], "fs2-control-db")
        self.assertEqual(client["egress_tcp_port"], 5432)
        self.assertIn('sai20_storage_reconciler_v3_component = "storage-reconciler-v3"', self.network)
        for label in client["required_generation_labels"]:
            self.assertRegex(
                self.network,
                rf'key\s+=\s+"{re.escape(label)}"\s+operator\s+=\s+"Exists"',
            )

    def test_admission_matches_direct_pods_and_every_builtin_template_shape(self) -> None:
        self.assertIn('resources   = ["pods", "replicationcontrollers"]', self.custody)
        self.assertIn(
            'resources   = ["deployments", "statefulsets", "daemonsets", "replicasets"]',
            self.custody,
        )
        self.assertIn('resources   = ["jobs", "cronjobs"]', self.custody)
        self.assertIn("object.metadata.labels", self.custody)
        self.assertIn("object.spec.template.metadata.labels", self.custody)
        self.assertIn("object.spec.jobTemplate.spec.template.metadata.labels", self.custody)
        self.assertNotRegex(self.custody, r"\n\s*objectSelector\s*=")

    def test_controller_identity_never_authorizes_an_unowned_direct_pod(self) -> None:
        self.assertIn("variables.controllerWriter && variables.controllerOwnedChild", self.custody)
        self.assertIn("object.metadata.ownerReferences.exists", self.custody)
        self.assertIn("'ReplicaSet', 'StatefulSet', 'DaemonSet', 'Job', 'ReplicationController'", self.custody)
        self.assertIn("request.resource.resource == 'replicasets' && owner.kind == 'Deployment'", self.custody)
        self.assertIn("request.resource.resource == 'jobs' && owner.kind == 'CronJob'", self.custody)

    def test_rejected_sai03_source_cannot_satisfy_the_acceptance_handoff(self) -> None:
        rejected = "cea63190aca6548d8be961a9432cc7cc1277721e"
        self.assertGreaterEqual(self.custody.count(rejected), 2)
        self.assertIn('status == "ACCEPTED"', self.custody)
        for receipt in (
            "independent_review_receipt_sha256",
            "rbac_census_receipt_sha256",
            "impersonation_guard_receipt_sha256",
        ):
            self.assertIn(receipt, self.custody)

    def test_policy_and_all_custody_objects_are_name_protected(self) -> None:
        for name in (
            "fs2-control-db-ingress",
            "fs2-database-client-workload-custody",
            "fs2-database-client-workload-custody-binding",
            "fs2-database-network-object-custody",
            "fs2-database-network-object-custody-binding",
            "fs2-database-client-workload-writer",
        ):
            self.assertIn(name, self.custody + self.network)
        self.assertIn("request.operation != 'DELETE'", self.custody)
        self.assertIn("security.fs2.nebius.ai/database-network-custody", self.network)

    def test_namespace_binding_covers_system_and_observability(self) -> None:
        self.assertIn('toset(["fs2-system", "fs2-observability"])', self.custody)
        self.assertIn('key      = "kubernetes.io/metadata.name"', self.custody)
        self.assertIn('operator = "In"', self.custody)
        self.assertIn("sai20_database_client_workload_writer", self.custody)


if __name__ == "__main__":
    unittest.main()
