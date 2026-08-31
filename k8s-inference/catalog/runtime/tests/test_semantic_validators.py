from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from validators.validate_response import SemanticError, validate


CATALOG_ROOT = Path(__file__).resolve().parents[1]


class SemanticValidatorTests(unittest.TestCase):
    def response(self, root: Path, name: str, content: str) -> Path:
        path = root / name
        path.write_text(json.dumps({"choices": [{"message": {"content": content}}]}) + "\n")
        return path

    def test_qwen_and_glm_exact_contracts_execute(self) -> None:
        cases = (
            ("qwen3-8b.json", "QWEN3_FS2_FIRST_OK", "QWEN3_FS2_SECOND_OK"),
            ("glm-5-2-fp8.json", "GLM52_FS2_FIRST_OK", "reasoning GLM52_FS2_SECOND_OK_42"),
        )
        for fixture, first, second in cases:
            with self.subTest(fixture=fixture), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                contract = json.loads((CATALOG_ROOT / "validators" / "assets" / fixture).read_text())
                result = validate(
                    contract,
                    [self.response(root, "one.json", first), self.response(root, "two.json", second)],
                )
                self.assertEqual("PASS", result["status"])

    def test_distinctness_and_oracles_fail_closed(self) -> None:
        contract = json.loads(
            (CATALOG_ROOT / "validators" / "assets" / "qwen3-8b.json").read_text()
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(SemanticError, "exact oracle"):
                validate(
                    contract,
                    [
                        self.response(root, "one.json", "wrong"),
                        self.response(root, "two.json", "QWEN3_FS2_SECOND_OK"),
                    ],
                )
            duplicate = copy.deepcopy(contract)
            duplicate["requests"][1]["request"] = duplicate["requests"][0]["request"]
            with self.assertRaisesRegex(SemanticError, "distinct"):
                validate(
                    duplicate,
                    [
                        self.response(root, "three.json", "QWEN3_FS2_FIRST_OK"),
                        self.response(root, "four.json", "QWEN3_FS2_SECOND_OK"),
                    ],
                )

    def test_cxr_contract_cannot_drop_nonclinical_noncommercial_policy(self) -> None:
        contract = json.loads(
            (CATALOG_ROOT / "validators" / "assets" / "nv-reason-cxr-3b.json").read_text()
        )
        contract["commercial_use"] = "allowed"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.response(root, "one.json", "x"),
                self.response(root, "two.json", "y"),
            ]
            with self.assertRaisesRegex(SemanticError, "nonclinical/noncommercial"):
                validate(contract, paths)

    def test_cxr_requires_closed_sections_and_declared_content_minima(self) -> None:
        contract = json.loads(
            (CATALOG_ROOT / "validators" / "assets" / "nv-reason-cxr-3b.json").read_text()
        )
        valid = (
            "<thinking>The lungs and pleural spaces are reviewed with the heart and "
            "cardiomediastinal silhouette. No focal airspace opacity, pleural effusion, "
            "or acute osseous abnormality is identified on this frontal examination.</thinking>"
            "<answer>No acute cardiopulmonary finding; lungs are clear and heart size is normal.</answer>"
        )
        pneumothorax = (
            "<think>The left lung has a visible pleural line with absent peripheral lung "
            "markings, consistent with pneumothorax. There is mediastinal shift and tracheal "
            "deviation toward the right, raising concern for tension physiology.</think>"
            "<answer>Left tension pneumothorax with rightward mediastinal shift.</answer>"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.response(root, "valid-one.json", valid),
                self.response(root, "valid-two.json", pneumothorax),
            ]
            self.assertEqual("PASS", validate(contract, paths)["status"])
            adversaries = {
                "unclosed": valid.replace("</thinking>", ""),
                "short-reasoning": "<thinking>lungs heart pleural normal</thinking><answer>No acute finding.</answer>",
                "few-medical-terms": (
                    "<thinking>" + "normal appearance without acute abnormality " * 8 + "</thinking>"
                    "<answer>No acute finding.</answer>"
                ),
            }
            for name, bad in adversaries.items():
                with self.subTest(name=name), self.assertRaises(SemanticError):
                    validate(
                        contract,
                        [self.response(root, f"{name}.json", bad), paths[1]],
                    )


if __name__ == "__main__":
    unittest.main()
