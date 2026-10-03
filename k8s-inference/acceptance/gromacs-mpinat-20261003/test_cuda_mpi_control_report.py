import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cuda_mpi_control_report import checkpoint_dump, compare, energy_blocks, printed_range_screen


def record(step):
    text = f"           Step           Time\n{step:15d}{step * .002:15.5f}\n\n   Energies (kJ/mol)\n"
    text += "".join(f"{s:>15}" for s in ("Potential", "Temperature", "Constr. rmsd")) + "\n"
    return text + "    -1.20000e+06    3.00000e+02    1.60000e-05\n\n"


class NativeNumericalTests(unittest.TestCase):
    def test_initial_final_decomposition(self):
        values = energy_blocks(record(0) + record(10000))
        self.assertEqual([x["step"] for x in values], [0, 10000])
        self.assertEqual(values[0]["values"]["Potential"], -1200000)

    def test_averages_are_not_mislabelled_final_frame(self):
        text = record(0) + record(10000)
        text += "Statistics over 10001 steps using 101 frames\n"
        text += record(10000).split("   Energies", 1)[1].join(["   Energies", ""])
        self.assertEqual(len(energy_blocks(text)), 2)
        values = energy_blocks(text, True)
        self.assertEqual(values[-1]["statistics_frames"], 101)
        self.assertIsNone(values[-1]["step"])

    def test_nonfinite_or_wrong_step_rejected(self):
        for text in (record(0) + record(9000), (record(0) + record(10000)).replace("3.00000e+02", "nan")):
            with self.assertRaises(ValueError):
                energy_blocks(text)

    def pair(self):
        blocks = energy_blocks(record(0) + record(10000))
        result = {"status": "passed", "input_sha256": "bundle", "request_sha256": "request",
                  "original_tpr_sha256": "tpr", "finite_tpr_sha256": "finite", "rates": {"mean": 100},
                  "records": [{"repeat": i, "command": ["gmx", "mdrun"], "kernel_lines": ["GPU 8x4"],
                               "energy_blocks": blocks, "final_gro_sha256": "gro"} for i in range(3)]}
        return copy.deepcopy(result), copy.deepcopy(result)

    def test_positive_pair_does_not_require_bitwise_final_state(self):
        a, b = self.pair()
        b["records"][0]["final_gro_sha256"] = "different"
        self.assertEqual(compare(a, b)["status"], "passed")
        self.assertFalse(compare(a, b)["pairs"][0]["final_coordinate_file_identical"])

    def test_changed_initial_science_rejected(self):
        a, b = self.pair()
        b["records"][0]["energy_blocks"][0]["values"]["Potential"] += 10
        self.assertEqual(compare(a, b)["status"], "failed")

    def test_changed_input_or_kernel_rejected(self):
        for field in ("input_sha256", "request_sha256", "original_tpr_sha256", "finite_tpr_sha256"):
            a, b = self.pair()
            b[field] += "changed"
            self.assertEqual(compare(a, b)["status"], "failed")
        a, b = self.pair()
        b["records"][0]["kernel_lines"] = ["CPU"]
        self.assertEqual(compare(a, b)["status"], "failed")

    def test_printed_unit_not_blanket_relative_tolerance(self):
        blocks = [energy_blocks(record(0) + record(10000))[0] for _ in range(3)]
        candidate = copy.deepcopy(blocks)
        candidate[0]["printed_tokens"]["Potential"] = "-1.19999e+06"
        candidate[0]["printed_tokens"]["Constr. rmsd"] = "1.60001e-05"
        result = printed_range_screen(blocks, candidate)
        self.assertEqual(result["screen_status"], "within_bounds")
        self.assertEqual(result["terms"]["Potential"]["one_printed_unit_margin"], 10)
        self.assertEqual(result["terms"]["Constr. rmsd"]["one_printed_unit_margin"], 1e-10)
        candidate[0]["printed_tokens"]["Constr. rmsd"] = "1.60002e-05"
        self.assertEqual(printed_range_screen(blocks, candidate)["screen_status"], "outside_bounds")

    def test_zero_term_keeps_absolute_printed_unit(self):
        blocks = [{"step": 0, "printed_tokens": {"term": "0.00000e+00"}} for _ in range(3)]
        candidate = [{"step": 0, "printed_tokens": {"term": "-1.00000e-05"}} for _ in range(3)]
        result = printed_range_screen(blocks, candidate)
        self.assertEqual(result["screen_status"], "within_bounds")
        self.assertEqual(result["terms"]["term"]["zero_baseline_values"], 3)
        self.assertFalse(result["scientific_equivalence_established"])

    def test_range_uses_all_baselines_not_paired_candidate_or_relative_scale(self):
        blocks = [{"step": 0, "printed_tokens": {"term": value}} for value in ("4.00000e+01", "4.00004e+01", "4.00002e+01")]
        candidate = [{"step": 0, "printed_tokens": {"term": "4.00005e+01"}} for _ in range(3)]
        result = printed_range_screen(blocks, candidate)
        self.assertEqual(result["screen_status"], "within_bounds")
        self.assertEqual(result["terms"]["term"]["inclusive_upper"], 40.0005)
        candidate[0]["printed_tokens"]["term"] = "4.00006e+01"
        self.assertEqual(printed_range_screen(blocks, candidate)["screen_status"], "outside_bounds")

    def test_screen_requires_complete_finite_initial_triplets(self):
        blocks = [{"step": 0, "printed_tokens": {"term": "1.00000e+00"}} for _ in range(3)]
        for changed in (blocks[:2], [{"step": 1, "printed_tokens": {"term": "1"}}] * 3,
                        [{"step": 0, "printed_tokens": {"other": "1"}}] * 3,
                        [{"step": 0, "printed_tokens": {"term": "NaN"}}] * 3):
            with self.assertRaises(ValueError):
                printed_range_screen(blocks, changed)

    def test_intermediate_checkpoint_read_requires_its_actual_expected_step(self):
        class Process:
            def __init__(self):
                self.stdout = io.BytesIO(b"checkpoint\nstep = 4000\nx[0] = 1.5\n")

            def wait(self, timeout):
                return 0

        with tempfile.TemporaryDirectory() as tmp, patch("cuda_mpi_control_report.subprocess.Popen", side_effect=lambda *args, **kwargs: Process()):
            root = Path(tmp)
            checkpoint = root / "state.cpt"
            checkpoint.write_bytes(b"retained intermediate native file")
            self.assertEqual(checkpoint_dump("exact-image", checkpoint, root / "header-1", expected_step=4000)["status"], "passed")
            self.assertEqual(checkpoint_dump("exact-image", checkpoint, root / "header-2")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
