"""Synthetic preparation/timeline controls only; no scientific simulation."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

import numpy as np

spec = importlib.util.spec_from_file_location("dense_gromacs_amber", Path(__file__).with_name("gromacs_amber.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DenseTests(unittest.TestCase):
    def test_mdp_only_whitelisted_fields_change(self):
        source = "nsteps = 500000\ndt = 0.002\ncontinuation = yes\ngen-vel = no\nld-seed = 20260925\nrcoulomb = 1.0\nnstxout-compressed = 500\nnstenergy = 500\nnstlog = 500\n"
        before = module.mdp_values(source)
        self.assertEqual(module.mdp_values(module.derive_mdp(source)), {**before, **module.GROMACS_CHANGES})
        self.assertEqual(before["nsteps"], "500000")

    def test_mdp_duplicate_or_bad_origin_rejected(self):
        with self.assertRaises(ValueError):
            module.mdp_values("dt=.002\ndt=.004")
        source = "nsteps=500000\ndt=0.002\ncontinuation=yes\ngen-vel=no\ninit-step=100\n"
        with self.assertRaises(ValueError):
            module.derive_mdp(source)

    def test_mdin_only_length_and_saved_cadence_change(self):
        source = "synthetic fixture only\n&cntrl\n nstlim=500000, dt=0.002, irest=1, ntx=5,\n ntpr=500, ntwx=500, ntwv=500, ig=20260925, ntwr=10000,\n barostat=1, baro_stochastic=1, gamma_ln=1.0,\n/\n&ewald\n nfft1=64, skinnb=2.0, skin_permit=0.5,\n/\n"
        self.assertEqual(module.mdin_values(module.derive_mdin(source)), {**module.mdin_values(source), **module.AMBER_CHANGES})

    def test_no_new_velocity_generation(self):
        with self.assertRaises(ValueError):
            module.derive_mdin("test\n&cntrl nstlim=500000, dt=0.002, irest=0, ntx=1, ntpr=500, ntwx=500, ntwv=500, /\n")
        with self.assertRaises(ValueError):
            module.derive_mdp("nsteps=500000\ndt=0.002\ncontinuation=yes\ngen-vel=yes\n")

    def test_request_is_short_native_checkpoint_workflow(self):
        gmx = module.request("gromacs")
        stages = gmx["jobs"][0]["steps"]
        self.assertEqual([s["command"] for s in stages], ["grompp", "mdrun", "energy", "trjcat", "check"])
        self.assertEqual(stages[1]["restart_checkpoint"], "source-final.cpt")
        self.assertIn("-notunepme", stages[1]["args"])
        amber = module.request("amber")["jobs"][0]["steps"]
        self.assertEqual(len(amber), 1)
        self.assertEqual(amber[0]["expected_nsteps"], 10000)
        self.assertEqual(amber[0]["coordinates"], "source-final.rst7")

    def test_strict_gromacs_native_dense_timeline(self):
        for first in (0, 10):
            steps = list(range(500000 + first, 510001, 10))
            times = (np.array(steps) * .002).astype(np.float32)
            self.assertEqual(module.frame_schedule(times, steps, "gromacs"), first == 0)

    def test_amber_float32_time_is_real_not_stored_step(self):
        times = (1200 + np.arange(10, 10001, 10) * .002).astype(np.float32)
        self.assertFalse(module.frame_schedule(times, [None] * 1000, "amber"))
        with self.assertRaises(ValueError):
            module.frame_schedule(times, list(range(10, 10001, 10)), "amber")

    def test_missing_duplicate_interpolated_cadence_rejected(self):
        times = 1000 + np.arange(10, 10001, 10) * .002
        steps = list(range(500010, 510001, 10))
        for altered in (times[:-1], np.r_[times[:500], times[499], times[501:]], times + .005):
            with self.assertRaises(ValueError):
                module.frame_schedule(altered, steps, "gromacs")
        steps[10] += 1
        with self.assertRaises(ValueError):
            module.frame_schedule(times, steps, "gromacs")

    def test_archive_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "inputs"
            inputs.mkdir()
            (inputs / "synthetic.txt").write_text("synthetic input, not a trajectory\n")
            module.archive(inputs, root / "a.tar.gz")
            module.archive(inputs, root / "b.tar.gz")
            self.assertEqual(module.sha(root / "a.tar.gz"), module.sha(root / "b.tar.gz"))

    def test_native_netcdf_restart_time_is_rank_zero(self):
        from scipy.io import netcdf_file
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic-restart.nc"
            with netcdf_file(path, "w") as target:
                variable = target.createVariable("time", "d", ())
                variable[...] = 1199.9999999854476
            with netcdf_file(path, "r", mmap=False) as source:
                self.assertEqual(module.netcdf_scalar(source.variables["time"]), 1199.9999999854476)


if __name__ == "__main__":
    unittest.main()
