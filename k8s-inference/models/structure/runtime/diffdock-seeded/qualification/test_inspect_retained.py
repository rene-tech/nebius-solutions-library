"""Offline evidence checks retain source warnings and numerical distinctions."""

import unittest

from inspect_retained import delta, parse
from rdkit import Chem, rdBase
from rdkit.Geometry import Point3D


def example():
    molecule = Chem.MolFromSmiles("CC")
    conformer = Chem.Conformer(2)
    conformer.SetAtomPosition(0, Point3D(0.0, 0.0, 1.0))
    conformer.SetAtomPosition(1, Point3D(1.5, 0.0, 1.0))
    conformer.Set3D(True)
    molecule.AddConformer(conformer)
    return molecule


class RetainedEvidenceTests(unittest.TestCase):
    def setUp(self):
        rdBase.LogToPythonStderr()

    def test_generated_3d_header_is_quiet(self):
        molecule, warning = parse(Chem.MolToMolBlock(example()))
        self.assertTrue(molecule.GetConformer().Is3D())
        self.assertEqual(warning, "")

    def test_missing_reference_header_emits_preserved_warning(self):
        original = Chem.MolToMolBlock(example())
        # Metadata-only analogue of ModelServer references; coordinates retained.
        missing = original.replace("3D", "  ", 1)
        molecule, warning = parse(missing)
        self.assertTrue(molecule.GetConformer().Is3D())
        self.assertIn("molecule is tagged as 2D", warning)
        self.assertEqual(molecule.GetConformer().GetAtomPosition(0).z, 1.0)

    def test_numerical_delta_not_relabelled_as_exact(self):
        left, right = example(), example()
        right.GetConformer().SetAtomPosition(0, Point3D(0.0016, 0.0, 1.0))
        comparison = delta([(left, 0.5)], [(right, 0.5002)])
        self.assertAlmostEqual(comparison["coordinate_angstrom"], 0.0016)
        self.assertAlmostEqual(comparison["confidence"], 0.0002)
        with self.assertRaisesRegex(ValueError, "Pose count"):
            delta([(left, 0.5)], [])


if __name__ == "__main__":
    unittest.main()
