"""All fixtures in this suite are SYNTHETIC, not simulation evidence."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from geometry import ValidationError, align_frame, dihedral, kabsch, make_whole, minimum_image, validate_cell
from native import Frame, frames, lammps_frames, validate_timeline
from thermo import native_performance, native_thermo, production_rows
from compare import analyze_run, comparison_table, plots, pressure_observations, sha256, validate_protocol


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.cell = np.diag([10., 11., 12.])

    def test_whole_bonded_peptide_crosses_boundary(self):
        raw = np.array([[9.8, 5, 5], [.8, 5, 5], [1.8, 5.5, 5]])
        original = raw.copy()
        result = make_whole(raw, [[0, 1], [1, 2]], self.cell)
        np.testing.assert_allclose(result, [[9.8, 5, 5], [10.8, 5, 5], [11.8, 5.5, 5]])
        np.testing.assert_equal(raw, original)

    def test_triclinic_nearest_image_against_brute_force(self):
        from itertools import product
        cell = np.array([[10., 0, 0], [4, 10, 0], [2, 1, 10]])
        rng = np.random.default_rng(37)
        for vector in rng.uniform(-18, 18, (30, 3)):
            expected = min((vector + np.array(t) @ cell for t in product(range(-4, 5), repeat=3)), key=np.linalg.norm)
            np.testing.assert_allclose(minimum_image(vector, cell), expected, atol=1e-10)

    def test_kabsch_proper_rotation_and_translation(self):
        p = np.array([[0., 0, 0], [1, 0, 0], [0, 2, 0], [0, 0, 1]])
        p -= p.mean(axis=0)
        rotation = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
        mobile = p @ rotation
        fit = kabsch(mobile, p)
        np.testing.assert_allclose(mobile @ fit, p, atol=1e-12)
        self.assertAlmostEqual(np.linalg.det(fit), 1)

    def test_kabsch_cannot_reflect_enantiomer(self):
        p = np.array([[0., 0, 0], [1, 0, 0], [0, 2, 0], [0, 0, 1]])
        p -= p.mean(axis=0)
        mirror = p * [-1, 1, 1]
        fit = kabsch(mirror, p)
        self.assertAlmostEqual(np.linalg.det(fit), 1)
        self.assertGreater(np.linalg.norm(mirror @ fit - p), .1)

    def test_dihedral_sign_against_mdanalysis(self):
        from MDAnalysis.lib.distances import calc_dihedrals
        rng = np.random.default_rng(6)
        for _ in range(30):
            p = rng.normal(size=(4, 3))
            expected = np.degrees(calc_dihedrals(*p))
            self.assertAlmostEqual(dihedral(p), expected, places=4)

    def test_water_periodic_image_and_common_alignment(self):
        peptide = np.array([[9.8, 5, 5], [10.8, 5, 5], [10.8, 6, 5], [10.8, 6, 6]])
        reference = peptide - peptide.mean(axis=0)
        all_positions = np.vstack((peptide % np.diag(self.cell), [[.9, 5, 5]]))
        p, w = align_frame(all_positions, self.cell, np.arange(4), [[0, 1], [1, 2], [2, 3]], np.arange(4), reference, [4])
        np.testing.assert_allclose(p, reference, atol=1e-10)
        self.assertLess(np.linalg.norm(w[0]), 2)

    def test_invalid_geometries_fail(self):
        for cell in (np.zeros((3, 3)), np.diag([10., 10., -10]), np.full((3, 3), np.nan), [[10, 0, 0], [10, 1, 0], [0, 0, 10]]):
            with self.assertRaises(ValidationError):
                validate_cell(cell)
        with self.assertRaises(ValidationError):
            make_whole([[0, 0, 0], [1, 0, 0], [2, 0, 0]], [[0, 1]], self.cell)
        with self.assertRaises(ValidationError):
            dihedral(np.zeros((4, 3)))
        with self.assertRaises(ValidationError):
            kabsch(np.zeros((4, 3)), np.zeros((4, 3)))


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-md-analysis-")
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_mda(self, extension, fmt, *, dt=1, n=3):
        import MDAnalysis as mda
        u = mda.Universe.empty(10, trajectory=True)
        path = self.directory / f"synthetic.{extension}"
        kwargs = {"dt": dt, "nsavc": 500, "istart": 100500} if fmt == "DCD" else {}
        with mda.Writer(str(path), format=fmt, n_atoms=10, **kwargs) as writer:
            for i in range(n):
                u.atoms.positions = np.arange(30).reshape(10, 3) * .1 + i
                u.dimensions = [40, 41, 42, 90, 90, 90]
                u.trajectory.ts.time = 201 + i
                u.trajectory.ts.data["step"] = 100500 + i * 500
                writer.write(u.atoms)
        return path

    def test_xtc_native_units_steps_time(self):
        path = self.write_mda("xtc", "XTC")
        values = list(frames(path, "gromacs", .002))
        self.assertEqual([f.step for f in values], [100500, 101000, 101500])
        self.assertEqual([f.time_ps for f in values], [201., 202., 203.])
        np.testing.assert_allclose(values[0].positions, np.arange(30).reshape(10, 3) * .1, atol=.011)
        self.assertFalse(list(self.directory.glob(".*offsets*")))
        validate_timeline(values, 100000, 200., 1500, 500, .002)

    def test_trr_native(self):
        path = self.write_mda("trr", "TRR")
        values = list(frames(path, "gromacs", .002, "TRR"))
        self.assertEqual(len(values), 3)
        np.testing.assert_allclose(values[0].cell, np.diag([40, 41, 42]), atol=1e-4)

    def test_namd_dcd_header_steps_time(self):
        path = self.write_mda("dcd", "DCD")
        values = list(frames(path, "namd", .002))
        validate_timeline(values, 100000, 200., 1500, 500, .002)
        self.assertEqual(values[0].step, 100500)
        self.assertAlmostEqual(values[0].time_ps, 201., places=4)

    def test_dcd_wrong_native_timestep_fails(self):
        path = self.write_mda("dcd", "DCD", dt=2)
        with self.assertRaises(ValidationError):
            list(frames(path, "namd", .002))

    def test_amber_native_netcdf(self):
        path = self.write_mda("nc", "NCDF")
        values = list(frames(path, "amber", .002))
        validate_timeline(values, 0, 200., 1500, 500, .002)
        self.assertTrue(all(f.step is None for f in values))
        np.testing.assert_allclose(values[-1].positions, np.arange(30).reshape(10, 3) * .1 + 2, atol=1e-5)

    def test_amber_missing_native_time_fails(self):
        from scipy.io import netcdf_file
        path = self.write_mda("nc", "NCDF")
        with netcdf_file(path, "a", mmap=False) as archive:
            del archive.variables["time"]
        with self.assertRaises(ValidationError):
            list(frames(path, "amber", .002))

    def test_truncated_native_xtc_fails_validation(self):
        path = self.write_mda("xtc", "XTC")
        path.write_bytes(path.read_bytes()[:-40])
        with self.assertRaises((ValidationError, OSError, EOFError)):
            values = list(frames(path, "gromacs", .002))
            validate_timeline(values, 100000, 200., 1500, 500, .002)

    def test_synthetic_end_to_end_analysis_and_plots(self):
        import MDAnalysis as mda
        u = mda.Universe.empty(10, trajectory=True)
        u.add_TopologyAttr("masses", np.full(10, 12.))
        positions = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [1, 1, 1], [2, 2, 2], [-2, 2, 2], [2, -2, 2], [2, 2, -2], [-2, -2, 2], [2, -2, -2]], dtype=float)
        path = self.directory / "synthetic-integration.xtc"
        with mda.Writer(str(path), n_atoms=10) as writer:
            for i in range(3):
                u.atoms.positions = positions + [39 + i, 20, 20]
                u.atoms.positions %= [40, 41, 42]
                u.dimensions = [40, 41, 42, 90, 90, 90]
                u.trajectory.ts.time = 201 + i
                u.trajectory.ts.data["step"] = 100500 + i * 500
                writer.write(u)
        thermo = self.directory / "synthetic.xvg"
        density = 120 / (40 * 41 * 42) * 1.66053906660 * 1000
        thermo.write_text('@ s0 legend "Temperature"\n@ s1 legend "Pressure"\n@ s2 legend "Density"\n' + "\n".join(f"{201 + i} 300 1 {density}" for i in range(3)))
        log = self.directory / "synthetic-production.log"
        log.write_text("SYNTHETIC UNIT FIXTURE\nPerformance: 123.45 0.19\n")
        topology = self.directory / "synthetic-topology.txt"
        topology.write_text("SYNTHETIC ten-atom analysis fixture, not scientific input")
        master = {"protocol": {"timestep_fs": 2., "production_steps": 1500, "output_every_steps": 500}, "manifest": {"atoms": 10, "total_charge_e": 0.}, "u": u, "peptide": np.arange(4), "bonds": np.array([[0, 1], [1, 2], [2, 3]]), "fit": np.arange(4), "reference": positions[:4] - positions[:4].mean(axis=0), "waters": np.arange(4, 10), "phi": [0, 1, 2, 3], "psi": [0, 1, 2, 3], "elements": ["C", "C", "N", "O"]}
        run = {"engine": "gromacs", "image": "synthetic/fixture@sha256:" + "0" * 64, "trajectory": str(path), "native_topology": str(topology), "production_log": str(log), "thermo": {"kind": "gromacs_xvg", "path": str(thermo)}, "canonical_to_native": "identity", "production_origin_step": 100000, "production_origin_time_ps": 200}
        output = self.directory / "synthetic-output"
        output.mkdir()
        summary = analyze_run(run, master, output / "gromacs")
        self.assertEqual(summary["frame_count"], 3)
        self.assertAlmostEqual(summary["performance"]["native_ns_per_day"], 123.45)
        self.assertIsNone(summary["initial_potential_energy_kJ_mol"])
        with np.load(output / "gromacs" / "display.npz") as values:
            np.testing.assert_allclose(values["peptide"][0], values["peptide"][-1], atol=.02)
        comparison_table(output, [summary])
        plots(output, ["gromacs"])
        self.assertEqual(len(list(output.glob("*.png"))), 4)

    def dump(self, ids="2 0.9 0.2 0.3\n1 0.1 0.2 0.3", bounds="0 10\n0 10\n0 10", header="pp pp pp"):
        path = self.directory / "synthetic.lammpstrj"
        path.write_text(f"ITEM: TIMESTEP\n500\nITEM: NUMBER OF ATOMS\n2\nITEM: BOX BOUNDS {header}\n{bounds}\nITEM: ATOMS id xs ys zs\n{ids}\n")
        return path

    def test_lammps_scaled_sorted(self):
        value = list(lammps_frames(self.dump(), .002))[0]
        np.testing.assert_allclose(value.positions, [[1, 2, 3], [9, 2, 3]])
        self.assertEqual(value.time_ps, 1)

    def test_lammps_triclinic_bounds(self):
        path = self.dump(bounds="0 13 2\n0 11 1\n0 10 1", header="xy xz yz pp pp pp")
        value = list(lammps_frames(path, .002))[0]
        np.testing.assert_allclose(value.cell, [[10, 0, 0], [2, 10, 0], [1, 1, 10]])
        np.testing.assert_allclose(value.positions[0], [1.7, 2.3, 3.])

    def test_lammps_duplicate_or_nonfinite_or_truncated_fail(self):
        for ids in ("1 0 0 0\n1 1 1 1", "1 0 0 0\n2 nan 1 1", "1 0 0 0"):
            with self.assertRaises(ValidationError):
                list(lammps_frames(self.dump(ids=ids), .002))

    def test_closed_lammps_segments_only_identical_boundary_deduped(self):
        one = self.directory / "segment1.lammpstrj"
        two = self.directory / "segment2.lammpstrj"
        template = self.dump().read_text()
        one.write_text(template.replace("\n500\n", "\n0\n") + template)
        two.write_text(template + template.replace("\n500\n", "\n1000\n"))
        boundaries = []
        iterator = frames([str(one), str(two)], "lammps", .002, boundary_receipts=boundaries)
        result = []
        for frame in iterator:
            frame.positions = None  # Match the actual memory-light consumer.
            result.append(frame)
        self.assertEqual([f.step for f in result], [0, 500, 1000])
        self.assertEqual([f.index for f in result], [0, 1, 2])
        self.assertEqual([b["step"] for b in boundaries], [500])
        validate_timeline(result, 0, 0, 1000, 500, .002)
        two.write_text(template.replace("2 0.9", "2 1.9") + template.replace("\n500\n", "\n1000\n"))
        boundaries = []
        self.assertEqual(len(list(frames([str(one), str(two)], "lammps", .002, boundary_receipts=boundaries))), 3)
        self.assertEqual(boundaries[0]["periodically_rewrapped_atoms"], 1)
        self.assertAlmostEqual(boundaries[0]["maximum_raw_position_difference_A"], 10)
        for invalid in (template.replace("2 0.9", "2 0.8") + template.replace("\n500\n", "\n1000\n"), template + template):
            two.write_text(invalid)
            with self.assertRaises(ValidationError):
                list(frames([str(one), str(two)], "lammps", .002))
        with self.assertRaises(ValidationError):
            list(frames([str(one), str(one)], "lammps", .002))

    def test_missing_duplicate_wrong_origin_timeline_fails(self):
        values = [Frame(i, None, None, i + 1., (i + 1) * 500, "synthetic") for i in range(3)]
        for bad in (values[:2], [values[0], values[0], values[2]], list(reversed(values))):
            with self.assertRaises(ValidationError):
                validate_timeline(bad, 0, 0., 1500, 500, .002)
        with self.assertRaises(ValidationError):
            validate_timeline(values, 500, 0., 1500, 500, .002)

    def test_initial_frame_included_explicitly(self):
        values = [Frame(i, None, None, float(i), i * 500, "synthetic") for i in range(4)]
        _, initial = validate_timeline(values, 0, 0., 1500, 500, .002)
        self.assertTrue(initial)


class ThermoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-thermo-")
        self.path = Path(self.temp.name) / "synthetic.log"

    def tearDown(self):
        self.temp.cleanup()

    def test_amber_footer_not_sample(self):
        self.path.write_text(" NSTEP = 500 TIME(PS) = 201.0 TEMP(K) = 300.0 PRESS = -23.0\n EPtot = -500.0 VOLUME = 20000.0 Density = 0.99\n NSTEP = 1000 TIME(PS) = 202.0 TEMP(K) = 301.0 PRESS = 21.0\n EPtot = -501.0 VOLUME = 20010.0 Density = 0.98\n A V E R A G E S\n NSTEP = 1000 TIME(PS) = 201.5 TEMP(K) = 300.5 PRESS = -1.0\n")
        rows = native_thermo(self.path, "amber_mdout", .002, 12000.)
        result = production_rows(rows, 0, 200, 1000, .002)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["pressure_bar"], -23)
        self.assertEqual(result[0]["potential_kJ_mol"], -2092)

    def test_amber_mc_pressure_placeholder_is_not_observation(self):
        self.path.write_text("| MONTE CARLO BAROSTAT IMPORTANT NOTE:\n| is that the reported pressure is always 0 because it is not calculated.\n NSTEP = 500 TIME(PS) = 201.0 TEMP(K) = 300.0 PRESS = 0.0\n EPtot = -500.0 VOLUME = 20000.0 Density = 0.99\n")
        row = native_thermo(self.path, "amber_mdout", .002, 12000.)[0]
        self.assertNotIn("pressure_bar", row)
        self.assertEqual(row["uncomputed_pressure_placeholder_bar"], 0)

    def test_pressure_join_requires_actual_bound_artifact(self):
        self.path.write_text("SYNTHETIC trajectory bytes")
        observations = self.path.with_suffix(".csv")
        observations.write_text("production_time_ps,pressure_bar\n1,-12\n2,33\n")
        provenance = self.path.with_suffix(".json")
        provenance.write_text('{"evidence":"SYNTHETIC pressure join unit fixture"}')
        spec = {"trajectory_sha256": sha256(self.path), "engine_image": "synthetic/fixture@sha256:" + "0" * 64, "method": "synthetic unit fixture only", "path": str(observations), "provenance_files": [str(provenance)]}
        values, receipt = pressure_observations(spec, self.path, [1, 2])
        self.assertEqual(values, {1: -12, 2: 33})
        self.assertEqual(len(receipt["files"]), 2)
        with self.assertRaises(ValidationError):
            pressure_observations(spec, self.path, [1, 3])
        with self.assertRaises(ValidationError):
            pressure_observations({**spec, "trajectory_sha256": "f" * 64}, self.path, [1, 2])

    def test_namd_units(self):
        self.path.write_text("ETITLE: TS TEMP PRESSURE VOLUME POTENTIAL\nENERGY: 500 300 1.2 20000 -100\nENERGY: 1000 301 -4 20001 -99\n")
        rows = native_thermo(self.path, "namd_log", .002, 12000.)
        self.assertAlmostEqual(rows[0]["potential_kJ_mol"], -418.4)
        self.assertEqual(rows[0]["pressure_bar"], 1.2)

    def test_namd_group_pressure_requires_native_declaration(self):
        self.path.write_text("Info: PRESSURE CONTROL IS GROUP-BASED\nETITLE: TS TEMP PRESSURE GPRESSURE VOLUME POTENTIAL\nENERGY: 500 300 25 -1.2 20000 -100\nENERGY: 1000 301 27 2.3 20001 -99\n")
        rows = native_thermo(self.path, "namd_log", .002, 12000.)
        self.assertEqual(rows[0]["pressure_bar"], -1.2)
        self.assertEqual(rows[0]["atomic_pressure_bar"], 25.)
        self.assertEqual(rows[0]["group_pressure_bar"], -1.2)
        self.path.write_text(self.path.read_text().replace("Info: PRESSURE CONTROL IS GROUP-BASED\n", ""))
        self.assertEqual(native_thermo(self.path, "namd_log", .002, 12000.)[0]["pressure_bar"], 25.)

    def test_lammps_real_units(self):
        self.path.write_text("Step Time Temp Press Volume Density PotEng\n500 1000 300 1 20000 0.99 -100\n1000 2000 301 -1 20010 0.98 -101\nLoop time of 2 on 4 procs for 1000 steps\n")
        rows = native_thermo(self.path, "lammps_log", .002, 12000.)
        self.assertEqual(rows[0]["time_ps"], 1)
        self.assertEqual(rows[0]["pressure_bar"], 1.01325)
        self.assertAlmostEqual(native_performance(self.path, "lammps", 1000, .002)["native_ns_per_day"], 86.4)

    def test_lammps_interleaved_shake_statistics_do_not_drop_rows(self):
        self.path.write_text("Step Time Temp Press Volume Density PotEng\n500 1000 300 1 20000 0.99 -100\nSHAKE/KK stats (type/ave/delta/count) on step 1000\nBond: 8 0.9572 1e-8 4384\nAngle: 12 104.491 1e-5 2192\n1000 2000 301 -1 20010 0.98 -101\nLoop time of 2 on 4 procs for 1000 steps\n3 4 5\n")
        rows = native_thermo(self.path, "lammps_log", .002, 12000.)
        self.assertEqual([row["step"] for row in rows], [500, 1000])
        self.path.write_text("Step Time Temp Press Volume Density PotEng\n500 1000 300 1 20000 0.99\n")
        with self.assertRaises(ValidationError):
            native_thermo(self.path, "lammps_log", .002, 12000.)

    def test_lammps_closed_thermo_boundary_preserves_both_observations(self):
        other = self.path.with_name("segment2.log")
        header = "Step Time Temp Press Volume Density PotEng\n"
        self.path.write_text(header + "0 0 300 1 20000 0.99 -100\n500 1000 301 2 20000 0.99 -101\nLoop time of 1 on 1 procs for 500 steps\n")
        other.write_text(header + "500 1000 302 3 20000 0.99 -101\n1000 2000 299 4 20000 0.99 -102\nLoop time of 2 on 1 procs for 500 steps\n")
        boundaries = []
        rows = native_thermo([str(self.path), str(other)], "lammps_log", .002, 12000., boundary_receipts=boundaries)
        self.assertEqual([row["step"] for row in rows], [0, 500, 1000])
        self.assertEqual(rows[1]["temperature_K"], 301)
        self.assertEqual(boundaries[0]["omitted_next_segment_initialization_observation"]["temperature_K"], 302)
        self.assertAlmostEqual(native_performance([str(self.path), str(other)], "lammps", 1000, .002)["native_ns_per_day"], 57.6)
        other.write_text(header + "500 1000 302 3 20000 0.99 -101\n500 1000 302 3 20000 0.99 -101\n")
        with self.assertRaises(ValidationError):
            native_thermo([str(self.path), str(other)], "lammps_log", .002, 12000.)

    def test_gromacs_labels_and_units(self):
        self.path.write_text('@ s0 legend "Temperature"\n@ s1 legend "Pressure"\n@ s2 legend "Density"\n1 300 1 998\n2 301 -1 997\n')
        rows = native_thermo(self.path, "gromacs_xvg", .002, 12000.)
        self.assertEqual(rows[0]["density_g_cm3"], .998)
        self.path.write_text("1 300 1 998\n")
        with self.assertRaises(ValidationError):
            native_thermo(self.path, "gromacs_xvg", .002, 12000.)

    def test_missing_final_thermo_and_duplicate_fail(self):
        rows = [{"step": 500., "time_ps": 1., "temperature_K": 300.}]
        for bad in (rows, rows * 2):
            with self.assertRaises(ValidationError):
                production_rows(bad, 0, 0., 1000, .002)

    def test_performance_missing_is_null_not_fabricated(self):
        self.path.write_text("SYNTHETIC no performance footer")
        self.assertIsNone(native_performance(self.path, "namd", 500000, .002)["native_ns_per_day"])

    def test_amber_all_steps_timing_not_last_window(self):
        self.path.write_text("| Average timings for last 200 steps:\n| Elapsed(s) = 2.0 Per Step(ms) = 10\n| ns/day = 999 seconds/ns = 5\n| Average timings for all steps:\n| Elapsed(s) = 118.38 Per Step(ms) = 0.24\n| ns/day = 729.84 seconds/ns = 118.38\n")
        result = native_performance(self.path, "amber", 500000, .002)
        self.assertEqual(result["native_ns_per_day"], 729.84)
        self.assertEqual(result["native_loop_seconds"], 118.38)

    def test_namd_accumulated_performance_not_startup_benchmark(self):
        self.path.write_text("Info: Benchmark time: 4 CPUs 0.000410 s/step\nPERFORMANCE: 604500 averaging 415.909 ns/day, 0.000415476 sec/step with standard deviation 0.1\nPERFORMANCE: 605000 averaging 415.908 ns/day, 0.000415476 sec/step with standard deviation 0.1\n")
        result = native_performance(self.path, "namd", 500000, .002)
        self.assertEqual(result["native_ns_per_day"], 415.908)
        self.assertEqual(result["native_final_timing_step"], 605000)

    def test_canonical_protocol_cannot_shorten_run(self):
        with self.assertRaises(ValidationError):
            validate_protocol({"production_steps": 500})


class RenderingTests(unittest.TestCase):
    def test_real_encoder_on_labeled_synthetic_geometry(self):
        from render import SETTINGS, compose_grid, render_clip
        with tempfile.TemporaryDirectory(prefix="synthetic-render-") as temporary:
            peptide = np.array([[[0., 0, 0], [1., 0, 0], [1., 1, 0], [1., 1, 1]]] * 3)
            water = np.random.default_rng(7).normal(size=(3, 20, 3)) * 4
            data = {"peptide": peptide, "water": water, "time_ps": np.array([1., 2., 3.]), "bonds": np.array([[0, 1], [1, 2], [2, 3]]), "elements": np.array(["C", "C", "N", "O"])}
            result = render_clip(data, "SYNTHETIC", Path(temporary) / "synthetic.mp4", settings={**SETTINGS, "pixels": 240}, synthetic=True)
            self.assertEqual(int(result["nb_read_frames"]), 3)
            grid = compose_grid([Path(temporary) / "synthetic.mp4"] * 4, Path(temporary) / "synthetic-grid.mp4", 3, settings={**SETTINGS, "pixels": 240})
            self.assertEqual(grid["width"], 480)
            self.assertEqual(int(grid["nb_read_frames"]), 3)
            with self.assertRaises(ValidationError):
                compose_grid([Path(temporary) / "synthetic.mp4"] * 4, Path(temporary) / "wrong-grid.mp4", 4, settings={**SETTINGS, "pixels": 240})


if __name__ == "__main__":
    unittest.main()
