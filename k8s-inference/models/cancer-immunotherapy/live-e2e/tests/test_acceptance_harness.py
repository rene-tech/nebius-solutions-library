from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE.parent / "acceptance_harness.py"
SPEC = importlib.util.spec_from_file_location("cancer_acceptance_harness", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)


class AcceptanceHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = HARNESS.load_json(HARNESS.PLAN_PATH)

    def test_checked_in_plan_is_strict_and_covers_requested_models(self) -> None:
        HARNESS.validate_plan(self.plan)
        self.assertEqual(9, len(self.plan["models"]))
        self.assertEqual(
            self.plan["requested_models"],
            [model["model_id"] for model in self.plan["models"]],
        )
        self.assertEqual("preparation-only", self.plan["mode"])
        self.assertFalse(self.plan["target"]["preparation_mutations_allowed"])
        self.assertEqual(["b300", "forge"], self.plan["target"]["forbidden_targets"])

    def test_current_main_preflight_fails_closed_with_exact_blockers(self) -> None:
        report = HARNESS.preflight(self.plan)
        self.assertFalse(report["ready_for_execution"])
        self.assertEqual(0, report["admitted_profile_count"])
        blockers = {(item["gate"], item["code"], item["detail"]) for item in report["blockers"]}
        for model_id in self.plan["requested_models"]:
            self.assertIn(("workload-profile", "profile_missing", model_id), blockers)
            self.assertIn(("candidate-source-receipt", "candidate_unqualified", model_id), blockers)
        self.assertIn(
            (
                "admin-surface",
                "admin_scientific_routes_fixture_only",
                "fixture-only-pending-backend-integration",
            ),
            blockers,
        )
        self.assertIn(
            (
                "semantic-validator",
                "numeric_threshold_contracts_missing",
                "missing-reviewed-model-specific-contracts",
            ),
            blockers,
        )

    def test_candidate_revision_drift_is_never_execution_authority(self) -> None:
        report = HARNESS.preflight(self.plan)
        drift = {
            item["detail"].split(":", 1)[0]
            for item in report["observations"]
            if item["code"] == "candidate_not_execution_authority"
        }
        self.assertEqual(
            {"alphafold3", "bindcraft", "boltzgen", "protenix-v2", "rfdiffusion"},
            drift,
        )

    def test_render_is_deterministic_non_submittable_and_canonically_labelled(self) -> None:
        first = HARNESS.render_cases(self.plan, "prep-20260902")
        second = HARNESS.render_cases(self.plan, "prep-20260902")
        self.assertEqual(first, second)
        self.assertEqual(18, len(first["cases"]))
        self.assertFalse(first["mutations_permitted"])
        self.assertTrue(all(case["submission_state"] == "blocked-until-preflight-ready" for case in first["cases"]))
        required_labels = set(self.plan["cleanup"]["selector_labels"])
        for case in first["cases"]:
            self.assertEqual(required_labels, set(case["labels"]))
            self.assertEqual("fs2-live-acceptance", case["labels"]["app.kubernetes.io/managed-by"])
            self.assertEqual("unresolved", case["labels"]["fs2.nebius.ai/local-queue"])

    def test_render_rejects_unsafe_run_id(self) -> None:
        with self.assertRaises(HARNESS.HarnessError):
            HARNESS.render_cases(self.plan, "NOT_SAFE")

    @staticmethod
    def _write_esmf_output(root: Path, *, include_oracles: bool) -> None:
        pdb_lines = []
        for atom in range(1, 21):
            pdb_lines.append(
                f"ATOM  {atom:5d}  CA  GLY A{atom:4d}    "
                f"{atom:8.3f}{atom + 1:8.3f}{atom + 2:8.3f}  1.00 90.00           C\n"
            )
        (root / "result.pdb").write_text("".join(pdb_lines), encoding="utf-8")
        (root / "confidence.json").write_text(
            json.dumps({"plddt": [90.0, 91.0], "ptm": 0.8, "pae": [1.0]}) + "\n",
            encoding="utf-8",
        )
        ids = ["esmfold2-structure", "esmfold2-confidence"]
        if include_oracles:
            ids.extend(
                [
                    "esmfold2-length",
                    "esmfold2-confidence-oracle",
                    "esmfold2-topology",
                    "esmfold2-determinism",
                ]
            )
        receipt = {
            "schema": "fs2-serve.nebius.ai/scientific-semantic-validation-receipt/v1",
            "model_id": "esmfold2",
            "source_revision": "827ec128e4cdaf80f7d6f95fb367a08980b34918",
            "runtime_image_digest": "sha256:" + "1" * 64,
            "validator_id": "esmfold2-sequence-fold-v1",
            "status": "passed",
            "input_manifest_sha256": "2" * 64,
            "output_manifest_sha256": "3" * 64,
            "checks": [
                {"id": item, "status": "passed", "evidence_sha256": "4" * 64}
                for item in ids
            ],
        }
        (root / "semantic-validation-receipt.json").write_text(
            json.dumps(receipt) + "\n", encoding="utf-8"
        )

    def test_artifacts_require_structural_prechecks_and_authoritative_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_esmf_output(root, include_oracles=True)
            report = HARNESS.validate_artifacts(self.plan, "esmfold2", root)
            self.assertTrue(report["passed"])
            self.assertEqual([], report["missing_authoritative_oracle_checks"])

    def test_artifacts_fail_when_authoritative_oracle_checks_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_esmf_output(root, include_oracles=False)
            report = HARNESS.validate_artifacts(self.plan, "esmfold2", root)
            self.assertFalse(report["passed"])
            self.assertEqual(4, len(report["missing_authoritative_oracle_checks"]))

    def test_cluster_reader_rejects_any_non_get_verb_before_subprocess(self) -> None:
        with self.assertRaises(HARNESS.HarnessError):
            HARNESS._kubectl_json("k8s-inference-h100", Path("/tmp/none"), ["apply", "-f", "x"])


if __name__ == "__main__":
    unittest.main()
