"""Synthetic geometry only: these tests do not constitute native MD evidence."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from geometry import ValidationError
from motion_video import chirality, dense_data, jitter, lengths, presentation_data, project_display_bonds, settings_for, stabilize, triangular_filter
from native import Frame
from render import SETTINGS


class MotionTests(unittest.TestCase):
    def fixture(self, count=21):
        base = np.array([[0., 0, 0], [1., 0, 0], [0, 1., 0], [0, 0, 1.]])
        peptide = np.repeat(base[None], count, axis=0)
        peptide[:, 3, 2] += .04 * (-1.) ** np.arange(count)
        return {"peptide": peptide, "water": np.tile([[[3., 3., 3.]]], (count, 1, 1)),
                "time_ps": np.arange(count, dtype=float), "bonds": np.array([[0, 1], [0, 2], [0, 3]]), "elements": np.array(["C", "N", "C", "C"])}

    def test_five_frame_filter_constant_and_impulse(self):
        np.testing.assert_allclose(triangular_filter(np.ones((9, 2))), 1)
        impulse = np.zeros(9); impulse[4] = 1
        np.testing.assert_allclose(triangular_filter(impulse)[2:7], np.array([1, 2, 3, 2, 1]) / 9)

    def test_stabilize_preserves_inputs_geometry_and_chirality(self):
        data = self.fixture()
        before = {key: value.copy() for key, value in data.items()}
        for index in range(len(data["peptide"])):
            theta = index * .2
            rotation = np.array([[np.cos(theta), -np.sin(theta), 0], [np.sin(theta), np.cos(theta), 0], [0, 0, 1]])
            data["peptide"][index] = data["peptide"][index] @ rotation + index
            data["water"][index] = data["water"][index] @ rotation + index
        raw = {key: value.copy() for key, value in data.items()}
        result, error = stabilize(data)
        self.assertLess(error, 1e-12)
        for key in data:
            np.testing.assert_array_equal(data[key], raw[key])
        np.testing.assert_allclose(lengths(result["peptide"], data["bonds"]), lengths(before["peptide"], data["bonds"]), atol=1e-12)
        self.assertTrue(np.all(chirality(result["peptide"], [0, 1, 2, 3]) > 0))

    def test_display_smoothing_reduces_jitter_without_mirroring(self):
        data = self.fixture()
        original = data["peptide"].copy()
        result, receipt = presentation_data(data, [0, 1, 2, 3])
        self.assertLess(receipt["presentation"]["rms_second_difference_A"], receipt["original"]["rms_second_difference_A"])
        self.assertTrue(receipt["alanine_chirality_preserved"])
        self.assertLess(receipt["display_bond_projection_error_A"], 1e-6)
        np.testing.assert_array_equal(data["peptide"], original)
        self.assertFalse(np.array_equal(result["peptide"], original))

    def test_projection_preserves_requested_bond_lengths(self):
        data = self.fixture()
        target = lengths(data["peptide"], data["bonds"])
        result, error = project_display_bonds(data["peptide"] * .7, data["bonds"], target)
        np.testing.assert_allclose(lengths(result, data["bonds"]), target, atol=1e-6)
        self.assertLess(error, 1e-6)

    def test_collapsed_bond_rejected(self):
        with self.assertRaises(ValidationError):
            project_display_bonds(np.zeros((3, 2, 3)), np.array([[0, 1]]), np.ones((3, 1)))

    def test_mode_labels_cannot_confuse_native_with_averaged(self):
        self.assertIn("VISUALLY SMOOTHED", settings_for("presentation")["mode_label"])
        self.assertIn("not for scientific", settings_for("presentation")["timing_caption"])
        self.assertIn("ACTUAL NATIVE", settings_for("dense")["mode_label"])
        self.assertIn("no averaging", settings_for("dense")["timing_caption"])
        self.assertEqual(settings_for("dense")["frame_interval_ps"], .02)
        self.assertEqual(SETTINGS["frame_interval_ps"], 1)
        self.assertNotIn("mode_label", SETTINGS)

    def dense_fixture(self, missing=False, time_shift=0, atom_count=4):
        data = self.fixture(3)
        p = data["peptide"][0]
        master = {"manifest": {"atoms": 4}, "peptide": np.arange(4), "bonds": data["bonds"], "fit": np.arange(4),
                  "reference": p - p.mean(axis=0), "waters": np.array([], dtype=int), "elements": data["elements"]}
        run = {"engine": "gromacs", "trajectory": "/synthetic/not-native.xtc", "origin_time_ps": 1200., "origin_step": 600000, "canonical_to_native": "identity"}
        native = [Frame(index, p[:atom_count].copy(), np.diag([40., 40., 40.]), 1200 + index * .02 + time_shift, 600000 + index * 10, "synthetic unit fixture") for index in range(1001 if not missing else 1000)]
        return run, master, native

    def test_dense_selects_only_real_positive_times_no_averaging(self):
        run, master, native = self.dense_fixture()
        with patch("motion_video.frames", return_value=iter(native)):
            data, receipt = dense_data(run, master)
        self.assertEqual(len(data["time_ps"]), 1000)
        np.testing.assert_allclose(data["time_ps"], np.arange(1, 1001) * .02, atol=1e-10)
        self.assertFalse(receipt["temporal_smoothing"])
        self.assertFalse(receipt["interpolation"])
        self.assertLess(receipt["rigid_fit_bond_error_A"], 1e-10)

    def test_dense_rejects_missing_frame(self):
        run, master, native = self.dense_fixture(missing=True)
        with patch("motion_video.frames", return_value=iter(native)), self.assertRaises(ValidationError):
            dense_data(run, master)

    def test_dense_rejects_changed_time(self):
        run, master, native = self.dense_fixture(time_shift=.01)
        with patch("motion_video.frames", return_value=iter(native)), self.assertRaises(ValidationError):
            dense_data(run, master)

    def test_dense_rejects_changed_stored_step(self):
        run, master, native = self.dense_fixture()
        native[1].step += 1
        with patch("motion_video.frames", return_value=iter(native)), self.assertRaises(ValidationError):
            dense_data(run, master)

    def test_dense_rejects_duplicated_initial_frame(self):
        run, master, native = self.dense_fixture()
        native.insert(0, native[0])
        with patch("motion_video.frames", return_value=iter(native)), self.assertRaises(ValidationError):
            dense_data(run, master)

    def test_dense_rejects_changed_atom_count(self):
        run, master, native = self.dense_fixture(atom_count=3)
        with patch("motion_video.frames", return_value=iter(native)), self.assertRaises(ValidationError):
            dense_data(run, master)


if __name__ == "__main__":
    unittest.main()
