"""Offline input-contract regressions; these do not qualify GPU execution."""
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).with_name("umbrella_campaign.py")
SPEC = importlib.util.spec_from_file_location("campaign", PATH)
campaign = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(campaign)


class InputContractTests(unittest.TestCase):
    def test_unique_periodic_windows(self):
        centers = list(range(-180, 180, 15))
        self.assertEqual(len(centers), 24)
        self.assertEqual(len({x % 360 for x in centers}), 24)
        self.assertEqual(campaign.circular_delta(180, -180), 0)
        self.assertEqual(campaign.circular_delta(-179, 179), 2)

    def test_bias_units_and_unbiased_psi(self):
        parameters = campaign.pull_parameters(-165)
        self.assertEqual(float(parameters["pull-coord1-k"]), 200)
        self.assertEqual(float(parameters["pull-coord1-init"]), -165)
        self.assertEqual(float(parameters["pull-coord2-k"]), 0)
        for number in (1, 2):
            self.assertEqual(parameters[f"pull-coord{number}-geometry"], "dihedral")
            self.assertEqual(parameters[f"pull-coord{number}-rate"], "0")
            self.assertEqual(parameters[f"pull-coord{number}-start"], "no")

    def test_instantaneous_two_column_pull_output(self):
        parameters = campaign.pull_parameters(0)
        self.assertEqual(parameters["pull-ncoords"], "2")
        self.assertEqual(parameters["pull-nstxout"], "50")
        for name in ("pull-print-ref-value", "pull-print-com", "pull-print-components", "pull-xout-average"):
            self.assertEqual(parameters[name], "no")
        self.assertNotIn("pull-print-reference", parameters)

    def test_full_ordered_stages_and_native_checkpoint_handoff(self):
        request = campaign.workflow("window-00")
        steps = request["jobs"][0]["steps"]
        stages = [s["id"] for s in steps if s["command"] == "mdrun"]
        self.assertEqual(stages, ["minimize", "nvt", "npt", "production"])
        production = next(s for s in steps if s["id"] == "prepare-production")
        self.assertIn("fs2-npt.cpt", production["args"])
        self.assertFalse(any("-cpi" in s["args"] for s in steps))
        self.assertTrue(all("-notunepme" in s["args"] for s in steps if s["command"] == "mdrun"))

    def test_no_nvt_volume_invention(self):
        steps = campaign.workflow("window-00")["jobs"][0]["steps"]
        nvt = next(s for s in steps if s["id"] == "energies-nvt")
        npt = next(s for s in steps if s["id"] == "energies-npt")
        self.assertNotIn("Volume", nvt["stdin"])
        self.assertIn("Volume", npt["stdin"])


if __name__ == "__main__":
    unittest.main()
