"""SYNTHETIC constraints only: never counted as native scientific evidence."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from geometry import ValidationError
from native import Frame
from shake_boundary import KIND, SOURCE_REVISION, WORKER_IMAGE, load_policy, project_cluster, verify_projection


class ShakeSetupTests(unittest.TestCase):
    def setUp(self):
        self.masses = np.array([16., 1., 1., 12.])
        self.pairs = np.array([[0, 1], [0, 2], [1, 2]])
        self.lengths = np.array([1., 1., np.sqrt(2.)])
        x = np.array([[2., 2, 2], [3.000008, 2, 2], [2, 3.000008, 2], [5, 5, 5]])
        y = x.copy()
        y[:3] += project_cluster(x[:3], self.masses[:3], self.pairs, self.lengths)
        v = np.arange(12).reshape(4, 3) * .001
        self.before = Frame(1, x, np.diag([10., 11., 12.]), 1., 500, "synthetic", v.copy(), np.zeros(3))
        self.after = Frame(0, y, self.before.cell.copy(), 1., 500, "synthetic", v.copy(), np.zeros(3))
        self.policy = {"kind": KIND, "step": 500, "evidence_sha256": "synthetic-not-scientific-evidence", "source_revision": SOURCE_REVISION, "model": {"masses_Da": self.masses, "pairs_zero_based": self.pairs, "distances_A": self.lengths}}

    def test_position_only_projection_and_nonmutation(self):
        old, new = self.before.positions.copy(), self.after.positions.copy()
        receipt = verify_projection(self.before, self.after, self.policy)
        self.assertLess(receipt["metrics"]["maximum_component_difference_from_position_only_SHAKE_solution_A"], 1e-13)
        self.assertGreater(receipt["metrics"]["maximum_periodic_component_displacement_A"], 1e-7)
        np.testing.assert_equal(old, self.before.positions)
        np.testing.assert_equal(new, self.after.positions)

    def test_reject_small_rigid_translation_even_with_perfect_constraints(self):
        self.after.positions[:3] += 1e-6
        with self.assertRaisesRegex(ValidationError, "COM|position_only"):
            verify_projection(self.before, self.after, self.policy)

    def test_reject_constraint_preserving_small_rotation(self):
        com = np.average(self.after.positions[:3], weights=self.masses[:3], axis=0)
        angle = 1e-5
        rotation = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
        self.after.positions[:3] = (self.after.positions[:3] - com) @ rotation + com
        with self.assertRaisesRegex(ValidationError, "position_only"):
            verify_projection(self.before, self.after, self.policy)

    def test_reject_unconstrained_change(self):
        self.after.positions[3, 0] += 1e-6
        with self.assertRaisesRegex(ValidationError, "unconstrained"):
            verify_projection(self.before, self.after, self.policy)

    def test_reject_velocity_change_and_missing_velocity(self):
        for value in (None, self.after.velocities + 1e-12):
            other = deepcopy(self.after)
            other.velocities = value
            with self.assertRaisesRegex(ValidationError, "velocities"):
                verify_projection(self.before, other, self.policy)

    def test_reject_cell_origin_or_cell_change(self):
        for name in ("cell", "cell_origin"):
            other = deepcopy(self.after)
            setattr(other, name, getattr(other, name) + 1e-12)
            with self.assertRaisesRegex(ValidationError, "cell/origin"):
                verify_projection(self.before, other, self.policy)

    def test_reject_other_step(self):
        self.policy["step"] = 1000
        with self.assertRaisesRegex(ValidationError, "step"):
            verify_projection(self.before, self.after, self.policy)

    def test_reject_other_image_or_changed_diagnostic(self):
        with self.assertRaisesRegex(ValidationError, "exact LAMMPS image"):
            load_policy({"kind": KIND}, "different@sha256:" + "0" * 64, [], "/not-read")
        with tempfile.TemporaryDirectory(prefix="synthetic-shake-binding-") as directory:
            path = Path(directory) / "diagnostic.json"
            path.write_text("{}")
            with self.assertRaisesRegex(ValidationError, "evidence hash"):
                load_policy({"kind": KIND, "evidence_path": path, "evidence_sha256": "0" * 64}, WORKER_IMAGE, [], "/not-read")


if __name__ == "__main__":
    unittest.main()
