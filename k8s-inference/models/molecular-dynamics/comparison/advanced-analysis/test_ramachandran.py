"""Offline synthetic method tests, never scientific evidence for native runs."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

spec = importlib.util.spec_from_file_location("ramachandran", Path(__file__).with_name("ramachandran.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class CircularCoordinates(unittest.TestCase):
    def test_wrap_boundary_and_multiple_turns(self):
        np.testing.assert_array_equal(m.wrap([-540, -180, 180, 540, 179]), [-180, -180, -180, -180, 179])

    def test_nonfinite_rejected(self):
        with self.assertRaises(ValueError):
            m.wrap([np.nan])

    def test_half_open_basin_boundaries(self):
        phi = [-91, -90, -80, 0, 20, -80, -80]
        psi = [60, 60, -120, -60, 120, -150, -150.001]
        labels = np.argmax(m.indicators(phi, psi)[:, :5], axis=1)
        np.testing.assert_array_equal(labels, [0, 1, 2, 3, 4, 4, 1])

    def test_partition_and_combined_population(self):
        rng = np.random.default_rng(11)
        values = m.indicators(rng.uniform(-720, 720, 10000), rng.uniform(-720, 720, 10000))
        np.testing.assert_array_equal(values[:, :5].sum(axis=1), np.ones(10000))
        np.testing.assert_array_equal(values[:, 5], values[:, 0] + values[:, 1])

    def test_periodic_assignment(self):
        phi, psi = np.array([-145, -60, 55, 179]), np.array([135, -40, 63, -179])
        np.testing.assert_array_equal(m.indicators(phi, psi), m.indicators(phi + 360, psi - 720))

    def test_same_grid_mask_and_R_units(self):
        _, counts, p, energy, relative = m.histogram([-50, -50, 50], [-50, -50, 50])
        np.testing.assert_allclose(p.sum(), 1)
        self.assertEqual(np.count_nonzero(counts), 2)
        self.assertTrue(np.isnan(energy[counts == 0]).all())
        self.assertAlmostEqual(float(np.nanmax(relative)), .00831446261815324 * 300 * np.log(2), places=12)

    def test_histogram_plus_minus_180_same_bin(self):
        _, counts, *_ = m.histogram([-180, 180], [180, -180])
        self.assertEqual(counts[0, 0], 2)

    def test_bad_bin_width_rejected(self):
        with self.assertRaises(ValueError):
            m.histogram([0], [0], width=7)


class Correlations(unittest.TestCase):
    def test_analytic_AR1_factor_of_two(self):
        rho = .8 ** np.arange(1000)
        result = m.initial_monotone(rho, 1000, dt=2)
        self.assertAlmostEqual(result["g"], 9, places=10)
        self.assertAlmostEqual(result["tau_int_ps"], 9, places=10)
        self.assertAlmostEqual(result["Neff"], 1000 / 9, places=10)

    def test_independent_neff(self):
        result = m.initial_monotone([1, 0, 0, 0], 1000)
        self.assertEqual(result["g"], 1)
        self.assertEqual(result["tau_int_ps"], .5)
        self.assertEqual(result["Neff"], 1000)

    def test_constant_is_undefined_not_perfect(self):
        rho, variance = m.acf(np.zeros(1000))
        self.assertIsNone(rho)
        self.assertEqual(variance, 0)
        result = m.initial_monotone(rho, 1000)
        self.assertIsNone(result["Neff"])

    def test_rotation_invariant_circular_trace(self):
        radians = np.random.default_rng(7).normal(size=1000).cumsum() / 20
        a = np.column_stack((np.cos(radians), np.sin(radians)))
        b = np.column_stack((np.cos(radians + 2.1), np.sin(radians + 2.1)))
        np.testing.assert_allclose(m.acf(a)[0], m.acf(b)[0], atol=1e-12)

    def test_linear_not_cyclic_acf(self):
        x = np.array([1., 0, 0, 0, 0, 0])
        centered = x - x.mean()
        expected = np.correlate(centered, centered, "full")[len(x) - 1:]
        np.testing.assert_allclose(m.acf(x)[0], expected / expected[0], atol=1e-12)

    def test_monotone_pairs_and_negative_stop(self):
        result = m.initial_monotone([1, .8, .5, .6, .8, .5, -.2, 0, .9, .9], 100)
        self.assertEqual(result["positive_pairs"], 3)
        self.assertAlmostEqual(result["g"], -1 + 2 * (1.8 + 1.1 + 1.1))

    def test_estimation_lag_is_capped(self):
        result = m.initial_monotone(np.ones(100), 100)
        self.assertEqual(result["maximum_estimation_lag_ps"], 50)
        self.assertEqual(result["last_included_lag_ps"], 49)
        self.assertTrue(result["reached_available_lag_limit"])


class Bootstrap(unittest.TestCase):
    def test_reproducible_and_compositional(self):
        phi = np.tile([-140, -70, -80, 50, 150], 20)
        psi = np.tile([140, 140, -45, 60, -120], 20)
        values = m.indicators(phi, psi)
        a = m.circular_bootstrap(values, 10, 200, [42, 0, 10])
        b = m.circular_bootstrap(values, 10, 200, [42, 0, 10])
        np.testing.assert_array_equal(a, b)
        np.testing.assert_allclose(a[:, :5].sum(axis=1), 1)
        np.testing.assert_allclose(a[:, 5], a[:, 0] + a[:, 1])

    def test_circular_sampling_preserves_expected_mean(self):
        values = np.r_[np.zeros(83), np.ones(17)]
        boot = m.circular_bootstrap(values, 20, 10000, 17)
        self.assertAlmostEqual(float(boot.mean()), .17, delta=.01)

    def test_zero_visits_do_not_get_zero_CI(self):
        self.assertIsNone(m.population_interval(np.zeros(100), 0))
        self.assertIsNone(m.population_interval(np.ones(100), 1))
        self.assertIsNone(m.sampling_requirement(0, None, .05))

    def test_zero_bootstrap_free_energy_draws_not_discarded(self):
        values = np.r_[np.zeros(10), np.full(90, .2)]
        result = m.basin_free_energy(.2, .8, values, np.full(100, .8))
        self.assertTrue(result["upper_unbounded"])
        self.assertIsNone(result["interval_95"][1])
        self.assertEqual(result["zero_basin_bootstrap_fraction"], .1)

    def test_both_zero_ratio_is_unresolved(self):
        result = m.basin_free_energy(.2, .8, [0, .2], [0, .8])
        self.assertIsNone(result["interval_95"])
        self.assertEqual(result["both_zero_bootstrap_fraction"], .5)

    def test_short_or_inexact_blocks_rejected(self):
        with self.assertRaises(ValueError):
            m.circular_bootstrap(np.arange(10), 3, 20, 1)

    def test_censored_runs_not_invented_transitions(self):
        result = m.sample_runs([1, 1, 0, 1, 1])
        self.assertEqual(result["entries_after_start"], 1)
        self.assertEqual(result["exits_before_end"], 1)
        self.assertTrue(result["left_censored"] and result["right_censored"])

    def test_planning_scales_with_correlation(self):
        a = m.sampling_requirement(.2, 1, .05)
        b = m.sampling_requirement(.2, 10, .05)
        self.assertAlmostEqual(b["plug_in_total_ns"], 10 * a["plug_in_total_ns"])

    def test_js_is_symmetric_bounded(self):
        self.assertEqual(m.js_divergence([.5, .5], [.5, .5]), 0)
        self.assertAlmostEqual(m.js_divergence([1, 0], [0, 1]), np.log(2))
        self.assertAlmostEqual(m.js_divergence([.3, .7], [.7, .3]), m.js_divergence([.7, .3], [.3, .7]))


class FrozenSources(unittest.TestCase):
    def test_inventory_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input").write_text("original")
            m.save(root / "delivery-manifest.json", {"files": [{"path": "input", "bytes": 8, "sha256": m.sha(root / "input")}]})
            self.assertEqual(m.verify_delivery(root)["verified_files"], 1)
            (root / "input").write_text("modified")
            with self.assertRaisesRegex(ValueError, "inventory mismatch"):
                m.verify_delivery(root)

    def test_bonferroni_family_has_all_36_comparisons(self):
        summary = {e: {"basins": {b: {"population": 0 if b == "alphaL" else .2} for b in m.BASINS}} for e in m.ENGINES}
        boots = {e: {20: np.full((2000, 6), .2)} for e in m.ENGINES}
        angles = {e: ([-60, -60], [-45, -45]) for e in m.ENGINES}
        rows, _ = m.agreement(summary, boots, angles, 20)
        self.assertEqual(len(rows), 36)
        self.assertTrue(all(r["Bonferroni_36_family_interval"] is None for r in rows if r["basin"] == "alphaL"))


if __name__ == "__main__":
    unittest.main()
