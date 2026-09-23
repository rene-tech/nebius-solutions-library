"""Explicitly SYNTHETIC parser/guard fixtures, never scientific evidence."""
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from convergence_gromacs import diagnostic_mdp
from dispersion_diagnostic import tails
from geometry import ValidationError
from static_compare import amber_energy, gromacs_energy, lammps_energy, lammps_forces, namd_energy, normalized
from temperature_diagnostic import AMBER_FORTRAN_KB, uncertainty


class StaticParserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-static-parser-")
        self.path = Path(self.temporary.name) / "synthetic.txt"

    def tearDown(self):
        self.temporary.cleanup()

    def gromacs(self):
        terms = {"Bond": 1, "Angle": 2, "Proper Dih.": 3, "Per. Imp. Dih.": 4,
                 "LJ (SR)": 5, "LJ-14": 6, "Disper. corr.": -7,
                 "Coulomb (SR)": -8, "Coul. recip.": -9, "Coulomb-14": 10, "Potential": 7}
        return "\n".join(f'@ s{i} legend "{name}"' for i, name in enumerate(terms)) + "\n0 " + " ".join(str(value * 4.184) for value in terms.values()) + "\n"

    def test_gromacs_labeled_unit_and_decomposition(self):
        self.path.write_text(self.gromacs())
        result = gromacs_energy(self.path, "on")
        self.assertAlmostEqual(result["torsion"], 7)
        self.assertAlmostEqual(result["vdw_including_14_and_tail"], 4)
        self.assertAlmostEqual(result["electrostatic_including_14"], -7)
        self.assertAlmostEqual(result["potential"], 7)

    def test_gromacs_duplicate_or_nonzero_or_multiple_rows_rejected(self):
        original = self.gromacs()
        for invalid in (original + '@ s0 legend "Bond"\n', original.replace('\n0 ', '\n1 '), original + original.splitlines()[-1] + '\n'):
            self.path.write_text(invalid)
            with self.assertRaises(ValidationError):
                gromacs_energy(self.path, "on")

    def test_lammps_tail_already_in_vdw(self):
        self.path.write_text("Step Atoms E_bond E_angle E_dihed E_impro E_vdwl E_coul E_long E_tail PotEng\n0 6598 1 2 3 4 4 -8 1 -7 7\n")
        result = lammps_energy(self.path, "on")
        self.assertEqual(result["vdw_including_14_and_tail"], 4)
        self.assertEqual(result["potential"], 7)

    def test_lammps_wrong_atom_count_or_duplicate_header_rejected(self):
        for invalid in ("Step Atoms E_bond PotEng\n0 5 1 1\n", "Step Atoms E_bond PotEng PotEng\n0 6598 1 1 1\n"):
            self.path.write_text(invalid)
            with self.assertRaises(ValidationError):
                lammps_energy(self.path, "off")

    def test_namd_duplicate_identical_run0_is_explicit(self):
        header = "ETITLE: TS BOND ANGLE DIHED IMPRP VDW ELECT BOUNDARY MISC POTENTIAL\n"
        row = "ENERGY: 0 1 2 3 4 4 -7 0 0 7\n"
        self.path.write_text(header + row * 2)
        self.assertEqual(namd_energy(self.path, "on")["potential"], 7)
        for invalid in (header + row + row.replace(" 0 1 2 ", " 1 1 2 "), header + row.replace(" -7 0 0 ", " -7 1 0 ")):
            self.path.write_text(invalid)
            with self.assertRaises(ValidationError):
                namd_energy(self.path, "on")

    def amber(self):
        return "BOND = 1.0000 ANGLE = 2.0000 DIHED = 3.0000\nVDWAALS = 4.0000 EEL = -5.0000 HBOND = 0.0000\n1-4 VDW = 6.0000 1-4 EEL = -7.0000 RESTRAINT = 0.0000\n"

    def test_amber_14_eel_not_confused_with_eel(self):
        self.path.write_text(self.amber() * 2)
        result = amber_energy(self.path, "on")
        self.assertEqual(result["raw_terms_kcal_mol"]["EEL"], -5)
        self.assertEqual(result["electrostatic_including_14"], -12)
        self.assertEqual(result["vdw_including_14_and_tail"], 10)
        self.assertEqual(result["potential"], 4)
        self.assertIn("sum of native", result["potential_source"])

    def test_amber_missing_or_conflicting_or_additional_energy_rejected(self):
        for invalid in (self.amber().replace("1-4 EEL = -7.0000", ""), self.amber() + self.amber().replace("-7.0000", "-8.0000"), self.amber().replace("RESTRAINT = 0.0000", "RESTRAINT = 1.0000")):
            self.path.write_text(invalid)
            with self.assertRaises(ValidationError):
                amber_energy(self.path, "on")

    def test_nonfinite_or_unclosed_energy_rejected(self):
        with self.assertRaises(ValidationError):
            normalized("synthetic", "off", {"e": np.nan}, 0, 0, 0, 0, 0, 0)
        with self.assertRaises(ValidationError):
            normalized("synthetic", "off", {"e": 1}, 0, 0, 0, 0, 0, 1)

    def test_force_dump_requires_canonical_unique_ids_and_finite_values(self):
        header = "ITEM: TIMESTEP\n0\nITEM: NUMBER OF ATOMS\n6598\nITEM: BOX BOUNDS pp pp pp\n0 50\n0 50\n0 50\nITEM: ATOMS id x y z fx fy fz\n"
        body = "\n".join(f"{i} 1 2 3 4 5 6" for i in range(6598, 0, -1)) + "\n"
        self.path.write_text(header + body)
        positions, forces = lammps_forces(self.path)
        self.assertEqual(forces.shape, (6598, 3))
        np.testing.assert_equal(positions[0], [1, 2, 3])
        for invalid in (body.replace("6598 1 2", "6597 1 2"), body.replace("6598 1 2 3 4", "6598 1 2 3 nan"), body.rsplit("\n", 2)[0]):
            self.path.write_text(header + invalid)
            with self.assertRaises(ValidationError):
                lammps_forces(self.path)


class BoundedConvergenceTests(unittest.TestCase):
    def test_temperature_constant_uses_actual_native_module_binding(self):
        self.assertEqual(AMBER_FORTRAN_KB, 8.31441 / 4184.)
        self.assertAlmostEqual(AMBER_FORTRAN_KB, .00831441 / 4.184, places=18)
        self.assertNotEqual(AMBER_FORTRAN_KB, 1.380658e-23 * 6.0221367e23 / 4184.)

    def test_correlated_block_uncertainty_is_not_naive_sample_sem(self):
        rng = np.random.default_rng(601)
        values = np.zeros(1000)
        for i in range(1, 1000):
            values[i] = .8 * values[i - 1] + rng.normal()
        result = uncertainty(values + 298.)
        self.assertGreater(result["positive_window_statistical_inefficiency"], 3)
        self.assertEqual([row["blocks"] for row in result["block_diagnostics"]], [100, 50, 20, 10, 5])
        self.assertGreater(result["positive_window_SEM"], result["std"] / np.sqrt(1000))
        for invalid in ([1.] * 1000, [1, 2, np.nan], list(range(10))):
            with self.assertRaises(ValidationError):
                uncertainty(invalid)

    def test_tail_integrals_have_correct_sign_units_and_exclusions(self):
        result = tails([0, 0], [[2.]], [[3.]], [], 1000., 10.)
        self.assertAlmostEqual(result["bulk_C6_only"], -2 * np.pi * 8 / 3e6)
        self.assertAlmostEqual(result["bulk_C12_contribution"], 2 * np.pi * 12 / 9e12)
        self.assertAlmostEqual(result["bulk_C6_only"], result["distinct_nonexcluded_pairmean_C6_only"])
        for exclusions in ([[0, 1]], [[0, 0]], [[0, 1], [1, 0]]):
            with self.assertRaises(ValidationError):
                tails([0, 0], [[2.]], [[3.]], exclusions, 1000.)

    def test_only_requested_mesh_settings_change(self):
        original = "nsteps = 0\nconstraints = none\ncoulombtype = PME\ntcoupl = no\npcoupl = no\nDispCorr = no\nfourier-nx = 64\nfourier-ny = 64\nfourier-nz = 64\npme-order = 4\nrcoulomb = 1.0\n; synthetic\n"
        expected = original.replace("= 64", "= 96").replace("pme-order = 4", "pme-order = 6")
        self.assertEqual(diagnostic_mdp(original, 96), expected)
        for invalid in (original.replace("nsteps = 0", "nsteps = 1"), original + "nsteps = 0\n", original.replace("pcoupl = no", "pcoupl = Berendsen"), original.replace("fourier-nz = 64\n", "")):
            with self.assertRaises(ValueError):
                diagnostic_mdp(invalid, 96)
        with self.assertRaises(ValueError):
            diagnostic_mdp(original, 256)


if __name__ == "__main__":
    unittest.main()
