import copy
import unittest

from bind_candidates import SOLUTION, activation, bind
from test_activate_mpi import contracts


class BindingTests(unittest.TestCase):
    def test_fourteen_day_budget_plus_export_grace_is_bounded(self):
        execution, catalog, _, _ = contracts()
        proofs = {"gromacs": {"runtime_image": "registry.test/gromacs@sha256:" + "e" * 64,
                             "recorded_at": "2026-10-05T15:00:00Z", "tests": [{"native": "passed"}]}}
        _, desired = bind(catalog, execution, proofs, {"gromacs": "a" * 64}, active_deadline_seconds=1211400)
        row = next(row for row in desired["models"] if row["model_id"] == "gromacs")
        self.assertEqual(row["stages"][0]["active_deadline_seconds"], 14 * 24 * 3600 + 1800)
        with self.assertRaises(ValueError):
            bind(catalog, execution, proofs, {"gromacs": "a" * 64}, active_deadline_seconds=1211401)

    def test_only_selected_images_and_proof_references_change(self):
        execution, catalog, _, _ = contracts()
        before = copy.deepcopy((execution, catalog))
        proofs = {model: {"runtime_image": "registry.test/gromacs@sha256:" + digit * 64,
                          "recorded_at": "2026-10-03T12:00:00Z", "tests": [{"native": "passed"}],
                          "customer_ready": False}
                  for model, digit in (("gromacs", "e"), ("gromacs-mpi", "f"))}
        updated, desired = bind(catalog, execution, proofs, dict.fromkeys(proofs, "a" * 64))
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

    def test_mpi_only_keeps_single_identity_and_receipts(self):
        execution, catalog, _, _ = contracts()
        single = catalog["profiles"][1]
        single["qualification"]["public_completion_receipt_sha256"] = "1" * 64
        single["qualification"]["scheduler_eligibility_receipt_sha256"] = "2" * 64
        proofs = {"gromacs-mpi": {"runtime_image": "registry.test/gromacs@sha256:" + "f" * 64,
                                  "recorded_at": "2026-10-03T12:00:00Z", "tests": [{"native": "passed"}]}}
        updated, desired = bind(catalog, execution, proofs, {"gromacs-mpi": "3" * 64})
        self.assertEqual(desired["models"][:2], execution["models"][:2])
        unchanged = copy.deepcopy(updated["profiles"][:2])
        for profile, original in zip(unchanged, catalog["profiles"][:2]):
            # An unchanged App can receive a projected map reference; all its
            # execution identity and original qualification receipts survive.
            profile["qualification"]["execution_map_sha256"] = original["qualification"]["execution_map_sha256"]
            self.assertEqual(profile, original)
        self.assertEqual(updated["profiles"][2]["execution_identity"]["runtime_recipe_sha256"], "3" * 64)

    def test_invalid_selection_or_recipe_membership_is_rejected(self):
        execution, catalog, _, _ = contracts()
        for proofs, recipes in (({}, {}), ({"sibling": {}}, {"sibling": "a" * 64}),
                                ({"gromacs-mpi": {}}, {"gromacs": "a" * 64})):
            with self.subTest(proofs=proofs), self.assertRaises(ValueError):
                bind(catalog, execution, proofs, recipes)

    def test_cuda_recipe_is_an_explicit_additive_superset(self):
        original = activation.source_recipe(SOLUTION)
        candidate = activation.source_recipe(SOLUTION, mpi_cuda_aware=True)
        old = {row["path"]: row for row in original["files"]}
        new = {row["path"]: row for row in candidate["files"]}
        prefix = "models/molecular-dynamics/gromacs/runtime/"
        self.assertEqual(set(new) - set(old), {prefix + path for path in (
            "Containerfile.mpi-cuda-aware", "Containerfile.mpi-cuda-aware.dockerignore",
            "cuda-aware/verify_build.py", "cuda-aware/mpi_device_probe.c")})
        self.assertTrue(all(new[path] == row for path, row in old.items()))
        self.assertNotEqual(activation.digest(original), activation.digest(candidate))

    def test_rdma_recipe_includes_exact_capability_and_loader_sources(self):
        cuda = activation.source_recipe(SOLUTION, mpi_cuda_aware=True)
        rdma = activation.source_recipe(SOLUTION, mpi_rdma=True)
        old = {row["path"]: row for row in cuda["files"]}
        new = {row["path"]: row for row in rdma["files"]}
        prefix = "models/molecular-dynamics/gromacs/runtime/"
        self.assertEqual(set(new) - set(old), {prefix + path for path in (
            "Containerfile.mpi-rdma", "rdma/host_collectives.c",
            "rdma/memlock_probe.c", "rdma/ld.so.conf")})
        self.assertTrue(all(new[path] == row for path, row in old.items()))
        self.assertNotEqual(activation.digest(cuda), activation.digest(rdma))
        self.assertEqual(rdma, activation.source_recipe(SOLUTION, mpi_cuda_aware=True, mpi_rdma=True))
