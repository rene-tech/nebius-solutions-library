"""Synthetic method tests only; these are never scientific trajectory evidence."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

import numpy as np

SOURCE = Path(__file__).resolve().parents[1] / "equilibration.py"
spec = importlib.util.spec_from_file_location("equilibration", SOURCE)
eq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eq)


class Correlations(unittest.TestCase):
    def test_trace_matches_direct_biased_covariance(self):
        x = np.random.default_rng(8).normal(size=(31, 2))
        centered = x - x.mean(axis=0)
        cov = sum(np.correlate(centered[:, i], centered[:, i], "full")[30:] for i in range(2)) / 31
        np.testing.assert_allclose(eq.acf_trace(x), cov / cov[0], atol=5e-16)

    def test_circle_rotation_and_wrapping_invariance(self):
        angles = np.cumsum(np.random.default_rng(5).normal(size=1000) * 15)
        a = eq.circular_correlation(angles)
        b = eq.circular_correlation((angles + 123.4 + 180) % 360 - 180)
        np.testing.assert_allclose(a["acf"], b["acf"], atol=3e-15)
        self.assertAlmostEqual(a["Neff"], b["Neff"], places=10)
        self.assertAlmostEqual(a["tau_int_ps"], a["g"] / 2)

    def test_wrap_crossing_is_small_motion(self):
        angles = np.tile([179., -179., 178., -178.], 250)
        corr = eq.circular_correlation(angles)
        self.assertGreater(corr["resultant_length"], .999)
        self.assertAlmostEqual(abs(corr["circular_mean_degrees"]), 180.)

    def test_constant_series_has_no_effective_sample_claim(self):
        for values in (np.ones(1000), np.zeros(1000)):
            out = eq.correlation(values)
            self.assertIsNone(out["Neff"])
            self.assertIsNone(out["tau_int_ps"])
        self.assertIsNone(eq.circular_correlation(np.ones(1000) * 180)["Neff"])

    def test_ar1_inefficiency_within_sampling_tolerance(self):
        rng = np.random.default_rng(1984)
        x = rng.normal(size=50000)
        for i in range(1, len(x)):
            x[i] += .9 * x[i - 1]
        g = eq.correlation(x)["g"]
        self.assertLess(abs(g - 19), 5)

    def test_anticorrelation_does_not_claim_more_than_n(self):
        out = eq.correlation(np.tile([-1., 1.], 500))
        self.assertEqual(out["g"], 1.)
        self.assertEqual(out["Neff"], 1000)

    def test_nonfinite_rejected(self):
        with self.assertRaises(eq.ValidationError):
            eq.correlation([1., 2., np.nan, 4.])

    def test_time_unit_scales_tau_not_neff(self):
        x = np.random.default_rng(4).normal(size=1000)
        a, b = eq.correlation(x), eq.correlation(x, .02)
        self.assertAlmostEqual(a["Neff"], b["Neff"])
        self.assertAlmostEqual(a["tau_int_ps"] * .02, b["tau_int_ps"])


class Uncertainty(unittest.TestCase):
    def test_bootstrap_reproducible_and_constant(self):
        a = eq.block_bootstrap_means(np.ones(1000) * 7, 50, 200, np.random.default_rng(3))
        np.testing.assert_array_equal(a, np.ones(200) * 7)
        x = np.arange(1000.)
        a = eq.block_bootstrap_means(x, 50, 200, np.random.default_rng(3))
        b = eq.block_bootstrap_means(x, 50, 200, np.random.default_rng(3))
        np.testing.assert_array_equal(a, b)

    def test_large_change_is_not_stationarity_pass(self):
        x = np.random.default_rng(14).normal(size=1000) + np.r_[np.zeros(500), np.ones(500) * 10]
        out, _ = eq.scalar_diagnostics(x, np.random.default_rng(2), 200)
        self.assertGreater(out["second_minus_first_half"], 9)
        self.assertNotIn("no detected", out["stationarity"])

    def test_constant_density_not_independent_samples(self):
        out, boot = eq.scalar_diagnostics(np.ones(100), np.random.default_rng(1), 200)
        self.assertIsNone(boot)
        self.assertIsNone(out["mean_95pct_interval"])

    def test_one_block_half_window_has_no_zero_width_ci(self):
        out, _ = eq.scalar_diagnostics(np.arange(100.), np.random.default_rng(4), 200)
        block = next(b for b in out["blocks"] if b["block_ps"] == 50)
        self.assertIsNone(block["half_difference_95pct_interval"])


class NativeSemantics(unittest.TestCase):
    def test_namd_wrapper_binding_uses_sourced_native_controls(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            data = root / "runs/namd/data/alanine"
            data.mkdir(parents=True)
            for name in eq.STAGES:
                (data / f"fs2-{name}-part000001.namd").write_text(f'source "{name}.namd"\n')
            run = {"engine": "namd", "trajectory": "production.dcd", "thermo": {"path": "production.log"}, "production_log": "production.log"}
            stages = eq.stage_specs(root, run)
            self.assertEqual(Path(stages[0]["control"]).name, "nvt.namd")
            self.assertEqual(Path(stages[0]["wrapper"]).name, "fs2-nvt-part000001.namd")
            (data / "fs2-nvt-part000001.namd").write_text('source "other.namd"\n')
            with self.assertRaises(eq.ValidationError):
                eq.stage_specs(root, run)

    def temporary(self, content):
        context = tempfile.TemporaryDirectory()
        self.addCleanup(context.cleanup)
        path = Path(context.name) / "synthetic.xvg"
        path.write_text(content)
        return path

    def test_nvt_xvg_optional_density_not_fabricated(self):
        path = self.temporary('@ s0 legend "Potential"\n@ s1 legend "Temperature"\n@ s2 legend "Pressure"\n1 -400 300 -20\n')
        row = eq.gromacs_energy(path)[0]
        self.assertNotIn("density_g_cm3", row)
        self.assertEqual(row["pressure_bar"], -20.)

    def test_gromacs_density_kg_per_m3(self):
        path = self.temporary('@ s0 legend "Potential"\n@ s1 legend "Temperature"\n@ s2 legend "Pressure"\n@ s3 legend "Density"\n1 -400 300 1 985\n')
        self.assertEqual(eq.gromacs_energy(path)[0]["density_g_cm3"], .985)

    def test_amber_uncomputed_pressure_remains_missing(self):
        rows = [{"pressure_bar": 0., "temperature_K": 298.}]
        status = eq.amber_nvt_pressure_gap(rows, "ntp=0,", "PRESS = 0.0")
        self.assertIn("unavailable", status)
        self.assertNotIn("pressure_bar", rows[0])
        self.assertEqual(rows[0]["uncomputed_pressure_placeholder_bar"], 0.)

    def test_do_not_hide_real_amber_pressure(self):
        with self.assertRaises(eq.ValidationError):
            eq.amber_nvt_pressure_gap([{"pressure_bar": 1.}], "ntp=0", "PRESS=1")
        with self.assertRaises(eq.ValidationError):
            eq.amber_nvt_pressure_gap([{"pressure_bar": 0.}], "ntp=1", "PRESS=0")

    def test_schedule_gaps_duplicates_origin_rejected(self):
        eq.validate_schedule(np.arange(101), 100, "synthetic")
        eq.validate_schedule(np.arange(1, 101), 100, "synthetic")
        for invalid in (np.arange(100), np.r_[np.arange(1, 100), 99], np.arange(2, 102)):
            with self.assertRaises(eq.ValidationError):
                eq.validate_schedule(invalid, 100, "synthetic")

    def test_running_mean_resets_and_excludes_initialization(self):
        rows = [dict(stage="nvt", stage_time_ps=0, x=900), dict(stage="nvt", stage_time_ps=1, x=2),
                dict(stage="nvt", stage_time_ps=2, x=4), dict(stage="npt", stage_time_ps=0, x=800),
                dict(stage="npt", stage_time_ps=1, x=20)]
        out = eq.running_rows(rows, ["x"])
        self.assertNotIn("stage_running_mean_x", out[0])
        self.assertEqual(out[2]["stage_running_mean_x"], 3)
        self.assertEqual(out[-1]["stage_running_mean_x"], 20)
        self.assertEqual(out[-1]["stage_running_n_x"], 1)

    def test_join_retains_both_native_clocks(self):
        thermo = [dict(stage_time_ps=t, native_time_ps=t + 10, native_step=(t + 10) * 500, temperature_K=300.) for t in range(101)]
        coords = [dict(stage_time_ps=t + 1e-5, native_time_ps=t + 10 + 1e-5, native_step=(t + 10) * 500, backbone_rmsd_A=1.) for t in range(1, 101)]
        out = eq.join_observations(thermo, coords, {"stage": "nvt", "duration_ps": 100, "elapsed_origin_ps": 0})
        self.assertNotIn("backbone_rmsd_A", out[0])
        self.assertEqual(out[1]["native_thermo_time_ps"], 11.)
        self.assertEqual(out[1]["native_coordinate_time_ps"], 11.00001)

    def test_rmsd_rigid_transform(self):
        x = np.random.default_rng(2).normal(size=(7, 3))
        q, _ = np.linalg.qr(np.random.default_rng(3).normal(size=(3, 3)))
        self.assertGreater(np.linalg.det(q), 0)
        self.assertLess(eq.aligned_rmsd(x @ q + 50, x, np.arange(7)), 1e-13)

    def test_periodic_whole_peptide_geometry(self):
        x = np.array([[9.7, 5., 5.], [10.3, 5.8, 5.], [11., 6., 5.8], [11.8, 5.2, 6.]])
        bonds = np.array([[0, 1], [1, 2], [2, 3]])
        whole = eq.make_whole(x % 10, bonds, np.eye(3) * 10)
        np.testing.assert_allclose(whole, x)
        self.assertAlmostEqual(eq.dihedral(whole), eq.dihedral(x))


if __name__ == "__main__":
    unittest.main()
