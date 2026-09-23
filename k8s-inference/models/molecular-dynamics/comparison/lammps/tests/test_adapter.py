"""Synthetic algebra/serialization controls, never scientific dynamics proof."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapter import adapt, coefficient_key, interactions, read_data, write_data


def synthetic_water():
    return {"Masses": [["1", "16.0"], ["2", "1.008"]], "Atoms": [["1", "1", "1", "-0.834", "0", "0", "0"], ["2", "1", "2", "0.417", "0.9572", "0", "0"], ["3", "1", "2", "0.417", "-0.239987", "0.926627", "0"]], "Bond Coeffs": [["1", "harmonic", "553.0", "0.9572"], ["2", "harmonic", "553.000", "0.957200"], ["3", "harmonic", "553.0", "1.5136"]], "Bonds": [["1", "1", "1", "2"], ["2", "2", "1", "3"], ["3", "3", "2", "3"]], "Angle Coeffs": [], "Angles": [], "Dihedral Coeffs": [], "Dihedrals": []}


class AdapterTests(unittest.TestCase):
    def test_retains_hh_term_and_compacts_oh_coefficients(self):
        original = synthetic_water()
        result, proof = adapt(original, [[1, 2, 3]])
        self.assertEqual(len(result["Bond Coeffs"]), 2)
        self.assertEqual(interactions(result, "Bonds"), interactions(original, "Bonds"))
        self.assertEqual(result["Atoms"], original["Atoms"])
        self.assertEqual(proof["shake_bond_types"], [1])
        self.assertEqual(proof["water_hh_bonds_retained_unconstrained"], 1)
        self.assertEqual(len(result["Bonds"]), 3)

    def test_added_harmonic_angle_exact_zero_for_arbitrary_coordinates(self):
        result, proof = adapt(synthetic_water(), [[1, 2, 3]])
        coeff = result["Angle Coeffs"][0]
        k, theta = map(float, coeff[2:])
        self.assertEqual(Decimal(coeff[2]), Decimal(0))
        angles = np.linspace(0, np.pi, 51)
        np.testing.assert_equal(k * (angles - np.radians(theta))**2, np.zeros_like(angles))
        np.testing.assert_equal(-2 * k * (angles - np.radians(theta)), np.zeros_like(angles))
        oh, hh, theta = proof["water_geometry_A_degrees"][0]
        self.assertAlmostEqual(2 * oh * np.sin(np.radians(theta / 2)), hh, places=14)

    def test_missing_hh_rejected(self):
        data = synthetic_water()
        data["Bonds"].pop()
        with self.assertRaises((ValueError, KeyError)):
            adapt(data, [[1, 2, 3]])

    def test_duplicate_bond_rejected(self):
        data = synthetic_water()
        data["Bonds"].append(["4", "1", "1", "2"])
        with self.assertRaises(ValueError):
            adapt(data, [[1, 2, 3]])

    def test_non_isosceles_water_rejected(self):
        data = synthetic_water()
        data["Bond Coeffs"][1][-1] = "1.1"
        with self.assertRaises(ValueError):
            adapt(data, [[1, 2, 3]])

    def test_malformed_geometry_rejected(self):
        data = synthetic_water()
        data["Bond Coeffs"][2][-1] = "3.0"
        with self.assertRaises(ValueError):
            adapt(data, [[1, 2, 3]])

    def test_no_original_mutation(self):
        data = synthetic_water()
        original = deepcopy(data)
        adapt(data, [[1, 2, 3]])
        self.assertEqual(data, original)

    def test_serialization_reloads_exactly(self):
        data, _ = adapt(synthetic_water(), [[1, 2, 3]])
        with tempfile.TemporaryDirectory(prefix="synthetic-shake-adapter-") as temporary:
            path = Path(temporary) / "synthetic.data"
            header = ["SYNTHETIC UNIT FIXTURE", "3 atoms", "3 bonds", "0 angles", "0 dihedrals", "2 atom types", "3 bond types", "0 angle types", "0 dihedral types", "0 10 xlo xhi", "0 10 ylo yhi", "0 10 zlo zhi"]
            write_data(path, header, data)
            restored_header, restored = read_data(path)
            self.assertEqual(restored, data)
            self.assertIn("1 angles", restored_header)
            self.assertIn("2 bond types", restored_header)


if __name__ == "__main__":
    unittest.main()
