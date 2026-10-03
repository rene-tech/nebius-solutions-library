import copy
import unittest

from bind_candidates import bind
from test_activate_mpi import contracts


class BindingTests(unittest.TestCase):
    def test_only_selected_images_and_proof_references_change(self):
        execution, catalog, _, _ = contracts()
        before = copy.deepcopy((execution, catalog))
        proofs = {model: {"runtime_image": "registry.test/gromacs@sha256:" + digit * 64,
                          "recorded_at": "2026-10-03T12:00:00Z", "tests": [{"native": "passed"}],
                          "customer_ready": False}
                  for model, digit in (("gromacs", "e"), ("gromacs-mpi", "f"))}
        updated, desired = bind(catalog, execution, proofs, "a" * 64)
        self.assertEqual((execution, catalog), before)
        self.assertEqual(desired["snapshot_bundles"], execution["snapshot_bundles"])
        self.assertEqual(desired["models"][0], execution["models"][0])
        sibling = copy.deepcopy(updated["profiles"][0])
        sibling["qualification"]["execution_map_sha256"] = catalog["profiles"][0]["qualification"]["execution_map_sha256"]
        self.assertEqual(sibling, catalog["profiles"][0])
        for profile in updated["profiles"][1:]:
            self.assertEqual(profile["execution_identity"]["runtime_recipe_sha256"], "a" * 64)
            self.assertIsNone(profile["qualification"]["public_completion_receipt_sha256"])
            self.assertIsNone(profile["qualification"]["scheduler_eligibility_receipt_sha256"])
