"""Synthetic method/negative controls only; not native simulation evidence."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import validate_umbrella_native as v


class ScheduleChecks(unittest.TestCase):
    def test_exact_xtc_with_optional_initial(self):
        for initial in (False, True):
            frames = [v.Frame(i, None, None, step * .002, step, "synthetic")
                      for i, step in enumerate(range(0 if initial else 500, 1501, 500))]
            _, got = v.validate_timeline(frames, 0, 0, 1500, 500, .002)
            self.assertEqual(initial, got)

    def test_missing_duplicate_wrong_time_or_step_rejected(self):
        frames = [v.Frame(i, None, None, float(i), i * 500, "synthetic") for i in range(4)]
        cases = [frames[:-1], [frames[0], frames[1], frames[1], frames[3]]]
        for attr, value in (("time_ps", 2.02), ("step", 999)):
            mutant = copy.deepcopy(frames)
            setattr(mutant[2], attr, value)
            cases.append(mutant)
        for mutant in cases:
            with self.assertRaises(v.ValidationError):
                v.validate_timeline(mutant, 0, 0, 1500, 500, .002)

    def test_pull_0_1ps_and_initial(self):
        for initial in (False, True):
            rows = np.array([[i * .1, -179., 170.] for i in range(0 if initial else 1, 11)])
            steps, got = v.validate_pull_schedule(rows, steps=500)
            self.assertEqual(got, initial)
            self.assertEqual(steps[-1], 500)

    def test_pull_gap_duplicate_shift_units_rejected(self):
        rows = np.array([[i * .1, -179., 170.] for i in range(11)])
        cases = [rows[:-1], rows.copy(), rows.copy(), rows.copy()]
        cases[1][5, 0] = cases[1][4, 0]
        cases[2][:, 0] += .002
        cases[3][5, 1] = 200
        for mutant in cases:
            with self.assertRaises(v.ValidationError):
                v.validate_pull_schedule(mutant, steps=500)


class GeometryAndPrecision(unittest.TestCase):
    def setUp(self):
        self.xyz = np.array([[1., 0, 0], [0., 0, 0], [0., 1, 0], [0., 0, 1]])
        self.cell = np.eye(3) * 10
        self.master = {"manifest": {"atoms": 4}, "peptide": np.arange(4),
                       "bonds": np.array([[1, 0], [1, 2], [1, 3]]),
                       "chiral_indices": [0, 1, 2, 3], "reference_chirality": 1.}
        self.pairs, self.lengths = np.array([[1, 0]]), np.array([1.])

    def validate(self, xyz, cell=None):
        return v.molecular_geometry(xyz, self.cell if cell is None else cell,
                                    self.master, self.pairs, self.lengths)

    def test_periodic_whole_peptide_and_inputs_unchanged(self):
        wrapped = (self.xyz + 9.8) % 10
        before = wrapped.copy()
        whole, stats = self.validate(wrapped)
        self.assertAlmostEqual(stats["signed_chirality_A3"], 1.)
        self.assertLess(stats["constraint_error_A"], 1e-10)
        np.testing.assert_array_equal(wrapped, before)
        self.assertAlmostEqual(np.linalg.norm(whole[0] - whole[1]), 1.)

    def test_inversion_coplanar_and_broken_bond_rejected(self):
        mutants = []
        for z in (-1., 0.):
            xyz = self.xyz.copy(); xyz[3] = [0, .5, z]
            mutants.append(xyz)
        xyz = self.xyz.copy(); xyz[3, 2] = 3.; mutants.append(xyz)
        for xyz in mutants:
            with self.assertRaises(v.ValidationError):
                self.validate(xyz)

    def test_constraint_not_only_broad_bond_check(self):
        xyz = self.xyz.copy(); xyz[0, 0] += .0002
        with self.assertRaisesRegex(v.GateError, "constraints"):
            self.validate(xyz)

    def test_nonfinite_or_lefthanded_box_rejected(self):
        xyz = self.xyz.copy(); xyz[0, 0] = np.nan
        with self.assertRaises(v.ValidationError): self.validate(xyz)
        with self.assertRaises(v.ValidationError): self.validate(self.xyz, -self.cell)

    def test_periodic_difference_not_linear(self):
        self.assertAlmostEqual(float(v.circular_delta(179.999, -179.999)), -.002)
        self.assertAlmostEqual(float(v.circular_delta(-179.999, 179.999)), .002)

    def test_six_significant_figures_not_displayed_decimal_count(self):
        self.assertAlmostEqual(v.xvg_rounding_bound(170.03), .0005)
        self.assertAlmostEqual(v.xvg_rounding_bound(180), .0005)
        self.assertAlmostEqual(v.xvg_rounding_bound(9.2), .000005)

    def test_precision_bound_covers_coordinate_quantization(self):
        xyz = np.array([[0., 1, 0], [0., 0, 0], [1., 0, 0], [1., .5, .9]])
        original = v.dihedral(xyz)
        bound = v.dihedral_precision_bound(xyz, 8e-6, original)
        rng = np.random.default_rng(42)
        for _ in range(200):
            changed = xyz + rng.uniform(-8e-6, 8e-6, xyz.shape)
            error = abs(float(v.circular_delta(v.dihedral(changed), original)))
            self.assertLess(error, bound)
        self.assertLess(bound, .01)
        self.assertGreater(v.dihedral_precision_bound(xyz, 16e-6, original), bound)

    def test_bad_precision_and_collinearity_rejected(self):
        xyz = np.array([[0., 1, 0], [0., 0, 0], [1., 0, 0], [1., .5, .9]])
        with self.assertRaises(v.ValidationError): v.dihedral_precision_bound(xyz, .01, 30.)
        with self.assertRaises(v.ValidationError): v.dihedral_precision_bound(np.zeros((4, 3)), 1e-5, 30.)

    def test_cv_sign_column_or_time_shift_not_accepted_as_precision(self):
        xyz = np.array([[0., 1, 0], [0., 0, 0], [1., 0, 0], [1., .5, .9]])
        angle = v.dihedral(xyz)
        observed, residual, bound = v.compare_cv(xyz, 8e-6, float(f"{angle:.6g}"), "synthetic")
        self.assertLess(abs(residual), bound)
        for wrong in (-angle, angle + 10., angle + .1):
            with self.assertRaisesRegex(v.GateError, "coordinate-pull-correspondence"):
                v.compare_cv(xyz, 8e-6, wrong, "synthetic wrong coordinate")


class CompletionChecks(unittest.TestCase):
    def setUp(self):
        self.command = {"step_id": "production", "segment": 1, "checkpoint_step": 1000000,
                        "exit_code": 0, "command": ["gmx", "mdrun", "-notunepme"]}
        self.result = {"status": "succeeded", "error": None, "job_id": "window-00",
                       "completed_steps": ["production"], "commands": [self.command]}
        self.request = {"jobs": [{"id": "window-00", "steps": [{"id": "production"}]}]}
        self.log = "NVIDIA H100\n Step Time\n1000000 2000.00000\nWriting checkpoint, step 1000000 at today\nFinished mdrun on rank 0\n"

    def test_native_checkpoint_and_log_crosscheck(self):
        proof = v.validate_completion(self.result, self.request, [self.log])
        self.assertEqual(proof["checkpoint_steps"], [1000000])

    def test_failed_command_missing_stage_or_short_checkpoint_rejected(self):
        cases = []
        for key, value in (("exit_code", 1), ("checkpoint_step", 999999), ("segment", 2)):
            mutant = copy.deepcopy(self.result); mutant["commands"][0][key] = value; cases.append(mutant)
        mutant = copy.deepcopy(self.result); mutant["completed_steps"] = []; cases.append(mutant)
        for result in cases:
            with self.assertRaises(v.ValidationError): v.validate_completion(result, self.request, [self.log])

    def test_false_result_completion_not_enough(self):
        for log in (self.log.replace("2000.00000", "1999.00000"), self.log.replace("Finished mdrun", "Stopped run"), self.log + "LINCS WARNING"):
            with self.assertRaises(v.ValidationError): v.validate_completion(self.result, self.request, [log])


class FileAndBoundaryChecks(unittest.TestCase):
    def test_exact_topology_bytes_even_if_mutant_inventory_rehashed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); frozen = root / "original.top"; native = root / "system.top"
            frozen.write_text("synthetic original\n"); native.write_bytes(frozen.read_bytes())
            v.verify_topology(native, frozen)
            native.write_text("synthetic charge edit\n")
            record = {"path": "system.top", "sha256": v.sha256(native), "size_bytes": native.stat().st_size}
            v.verify_inventory(root, [record])
            with self.assertRaisesRegex(v.GateError, "topology"):
                v.verify_topology(native, frozen)

    def test_wrong_duration_cadence_or_averaging_controls_rejected(self):
        values = {"nsteps": 1000000, "dt": .002, "nstxout-compressed": 500,
                  "pull-nstxout": 50, "compressed-x-precision": 1000000,
                  "pull-xout-average": "no", "pull-print-ref-value": "no",
                  "pull-print-components": "no", "pull-print-com": "no", "pull": "yes", "pull-ncoords": 2}
        text = lambda d: "\n".join(f"{key} = {value}" for key, value in d.items())
        v.controls(text(values))
        for key, value in (("nsteps", 500000), ("pull-nstxout", 500), ("nstxout-compressed", 50),
                           ("pull-xout-average", "yes"), ("dt", .001), ("compressed-x-precision", 1000)):
            with self.assertRaises(v.ValidationError): v.controls(text({**values, key: value}))
        with self.assertRaises(v.ValidationError): v.controls(text(values) + "\nnsteps=1000000")

    def test_inventory_hash_size_duplicates_and_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "system.top").write_text("synthetic topology\n")
            record = {"path": "system.top", "sha256": v.sha256(root / "system.top"), "size_bytes": (root / "system.top").stat().st_size}
            self.assertEqual(len(v.verify_inventory(root, [record])), 1)
            for records in ([record, record], [{**record, "sha256": "0" * 64}], [{**record, "size_bytes": 1}], [{**record, "path": "../system.top"}]):
                with self.assertRaises(v.ValidationError): v.verify_inventory(root, records)

    def test_output_refuses_existing_and_native_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "native"; source.mkdir()
            with self.assertRaises(v.ValidationError): v.guard_output(source / "new", [source])
            with self.assertRaises(v.ValidationError): v.guard_output(source, [source])
            v.guard_output(root / "new-analysis", [source])

    def test_manifest_relative_and_explicit_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "window-00").mkdir()
            manifest = root / "list.json"; manifest.write_text(json.dumps({"windows": [{"path": "window-00"}]}))
            self.assertEqual(v.paths_from_manifest(manifest), [(root / "window-00").resolve()])

    def test_native_pull_columns_finite_and_legend(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pull.xvg"
            good = '@ s0 legend "1"\n@ s1 legend "2"\n0.0000 -179.3 170.2\n'
            path.write_text(good)
            self.assertEqual(v.pull_rows(path).shape, (1, 3))
            for bad in (good.replace('legend "1"', 'legend "2"'), good.replace("170.2", "nan"), good.replace("170.2", "170.2 3")):
                path.write_text(bad)
                with self.assertRaises(v.ValidationError): v.pull_rows(path)

    def test_source_concat_cannot_hide_changed_boundary_or_dropped_frame(self):
        from collections import namedtuple
        X = namedtuple("X", "x box step time prec")
        def frame(step): return X(np.array([[float(step), 0., 0.]], dtype=np.float32), np.eye(3, dtype=np.float32), step, step * .002, 1e6)
        a, b, c = frame(0), frame(500), frame(1000)
        canonical = [v.source_frame_fingerprint(f) for f in (a, b, c)]
        with patch.object(v, "xtc_values", side_effect=lambda p: iter({"a": [a, b], "b": [b, c]}[p])):
            self.assertEqual(len(v.validate_source_segments(["a", "b"], canonical)), 1)
        changed = b._replace(x=b.x + .0001)
        with patch.object(v, "xtc_values", side_effect=lambda p: iter({"a": [a, b], "b": [changed, c]}[p])):
            with self.assertRaises(v.ValidationError): v.validate_source_segments(["a", "b"], canonical)
        with patch.object(v, "xtc_values", return_value=iter([a, c])):
            with self.assertRaises(v.ValidationError): v.validate_source_segments(["a"], canonical)


class ExactInputBinding(unittest.TestCase):
    def fixture(self, prefix="window-01/"):
        args = ["-f", prefix + "production.mdp", "-p", prefix + "system.top",
                "-n", prefix + "dihedrals.ndx", "-o", "production.tpr"]
        step = {"id": "prepare-production", "command": "grompp", "args": args}
        command = {"step_id": "prepare-production", "command": ["gmx", "grompp", *args], "directory": "."}
        production = {"step_id": "production", "command": ["gmx", "mdrun", "-s", "production.tpr"], "directory": "."}
        result = {"job_id": "window-01", "commands": [command, production]}
        request = {"jobs": [{"id": "window-01", "steps": [step]}]}
        declared = {prefix + name for name in ("production.mdp", "system.top", "dihedrals.ndx")} | {"production.tpr"}
        return result, request, declared

    def bind(self, result, request, declared):
        return v.bind_native_inputs(result, request, Path("/synthetic/data"), Path("/synthetic/data"), declared)

    def test_mixed_batch_layout_resolves_exact_own_inputs(self):
        result, request, declared = self.fixture()
        declared |= {"window-02/production.mdp", "window-02/system.top", "window-02/dihedrals.ndx"}
        paths, proof = self.bind(result, request, declared)
        self.assertEqual(paths["-p"], Path("/synthetic/data/window-01/system.top"))
        self.assertEqual(paths["-o"], Path("/synthetic/data/production.tpr"))
        self.assertFalse(proof["cross_window_references_allowed"])

    def test_flat_canary_remains_supported(self):
        paths, _ = self.bind(*self.fixture(""))
        self.assertEqual(paths["-p"], Path("/synthetic/data/system.top"))

    def test_matching_request_cannot_authorize_cross_window_reference(self):
        with self.assertRaisesRegex(v.GateError, "cross-window"):
            self.bind(*self.fixture("window-02/"))

    def test_actual_and_requested_commands_must_match(self):
        result, request, declared = self.fixture()
        result["commands"][0]["command"][3] = "window-01/other.mdp"
        declared.add("window-01/other.mdp")
        with self.assertRaisesRegex(v.GateError, "differs from exact requested"):
            self.bind(result, request, declared)

    def test_inventory_binding_and_non_escaping_paths(self):
        result, request, declared = self.fixture()
        declared.remove("window-01/system.top")
        with self.assertRaises(v.GateError): self.bind(result, request, declared)
        with self.assertRaises(v.GateError): self.bind(*self.fixture("../window-01/"))

    def test_production_cannot_use_different_tpr(self):
        result, request, declared = self.fixture()
        result["commands"][1]["command"][-1] = "other.tpr"
        declared.add("other.tpr")
        with self.assertRaisesRegex(v.GateError, "different TPR"):
            self.bind(result, request, declared)


if __name__ == "__main__":
    unittest.main()
