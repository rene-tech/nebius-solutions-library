"""CPU regressions against exact upstream functions, without loading weights.

Run in the predecessor/candidate environment (OmegaConf is already installed):
COMPLEXA_SOURCE_ROOT=/upstream python /repair/tests/test_variant_repair.py
The /upstream input must be the unmodified pinned 54058860 source.
"""
from __future__ import annotations

import ast
from collections.abc import Sequence
import hashlib
import importlib.util
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from omegaconf import OmegaConf


HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("variant_patch", HERE / "patch_variant_runtime.py")
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)
UPSTREAM = Path(os.environ.get("COMPLEXA_SOURCE_ROOT", "/opt/fs2/source"))
DATASET = "src/proteinfoundation/datasets/gen_dataset.py"
REWARD = "src/proteinfoundation/rewards/rf3_reward.py"


def ligand_constructor(source):
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LigandFeatures")
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    # Execute the actual constructor through all normalization/combination
    # validation, stopping only before it opens the target structure.
    stop = next(i for i, n in enumerate(init.body)
                if isinstance(n, ast.If) and "os.path.exists" in ast.unparse(n.test))
    init.body = init.body[:stop]
    cls.body = [init]
    ns = {"Sequence": Sequence, "ConditionalFeature": type("ConditionalFeature", (), {})}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "actual_ligand_constructor", "exec"), ns)
    return ns["LigandFeatures"]


def predictor(source):
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RF3RewardRunner")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "predict_batch_from_files")
    ns = {"Any": object, "logger": logging.getLogger("variant-repair-test"), "os": os}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), "actual_rf3_prediction", "exec"), ns)
    return ns["predict_batch_from_files"]


class VariantRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.originals = {p: (UPSTREAM / p).read_text() for p in patcher.SOURCE_HASHES}

    def test_exact_upstream_bytes(self):
        for path, source in self.originals.items():
            self.assertEqual(hashlib.sha256(source.encode()).hexdigest(), patcher.SOURCE_HASHES[path])

    def test_actual_hydra_multiligand_fails_before_and_preserves_names_after(self):
        config = OmegaConf.load(UPSTREAM / "configs/design_tasks/ame_dict_v2.yaml")
        ligand = config.motif_target_dict_cfg.M0584_1ldm.ligand
        self.assertNotIsInstance(ligand, (list, tuple))
        self.assertIsInstance(ligand, Sequence)
        before = ligand_constructor(self.originals[DATASET])
        after = ligand_constructor(patcher.transform(DATASET, self.originals[DATASET]))
        with self.assertRaisesRegex(ValueError, "ligand is null/empty"):
            before("M0584_1ldm", "unused", ligand)
        self.assertEqual(after("M0584_1ldm", "unused", ligand)._res_names, ["NAD", "OXM"])

    def test_string_list_tuple_and_none_semantics_unchanged(self):
        fixed = ligand_constructor(patcher.transform(DATASET, self.originals[DATASET]))
        for ligand in ("FAD", ["FAD"], ("FAD",)):
            self.assertEqual(fixed("ligand", "unused", ligand)._res_names, ["FAD"])
        with self.assertRaisesRegex(ValueError, "ligand is null/empty"):
            fixed("empty", "unused", None)
        self.assertEqual(fixed("whole-file", "unused", None, ligand_only=True)._res_names, [])
        with self.assertRaisesRegex(ValueError, "Multi-ligand mode requires"):
            fixed("multi", "unused", OmegaConf.create(["NAD", "OXM"]), use_bonds_from_file=False)

    def test_rf3_real_exception_not_replaced_with_empty_prediction(self):
        original = predictor(self.originals[REWARD])
        fixed = predictor(patcher.transform(REWARD, self.originals[REWARD]))
        failure = RuntimeError("CuEq import failure")

        def fail(**kwargs):
            raise failure

        runner = SimpleNamespace(dump_dir="unused", predict_from_file=fail,
                                 _empty_prediction=lambda: {"output_cif_path": None})
        self.assertEqual(original(runner, ["input.pdb"]), [{"output_cif_path": None}])
        with self.assertRaisesRegex(RuntimeError, "RF3 prediction failed") as raised:
            fixed(runner, ["input.pdb"])
        self.assertIs(raised.exception.__cause__, failure)
        # Both native and SMILES-compatible handlers have the same repair.
        self.assertNotIn("predictions.append(self._empty_prediction())", patcher.transform(REWARD, self.originals[REWARD]))

    def test_successful_rf3_result_is_unchanged(self):
        fixed = predictor(patcher.transform(REWARD, self.originals[REWARD]))
        result = {"output_cif_path": "real.cif", "summary_confidence": [{"plddt": 0.67}]}
        runner = SimpleNamespace(dump_dir="unused", predict_from_file=lambda **kwargs: result)
        self.assertEqual(fixed(runner, ["input.pdb"]), [result])

    def test_patch_validates_all_inputs_before_editing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for p, source in self.originals.items():
                (root / p).parent.mkdir(parents=True, exist_ok=True)
                (root / p).write_text(source)
            (root / REWARD).write_text(self.originals[REWARD] + "\n")
            with self.assertRaisesRegex(ValueError, "Pinned upstream source mismatch"):
                patcher.patch(root)
            self.assertEqual((root / DATASET).read_text(), self.originals[DATASET])

    def test_patch_exact_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for p, source in self.originals.items():
                (root / p).parent.mkdir(parents=True, exist_ok=True)
                (root / p).write_text(source)
            patcher.patch(root)
            for p, source in self.originals.items():
                self.assertEqual((root / p).read_text(), patcher.transform(p, source))


if __name__ == "__main__":
    unittest.main()
