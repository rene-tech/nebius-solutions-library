"""CPU-only unit tests; actual native synthetic checks use validate-synthetic."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SPEC = importlib.util.spec_from_file_location("umbrella_wham", Path(__file__).with_name("umbrella_wham.py"))
wham = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wham)


def native_dump(*, center=-180, k=200, psi_k=0, ncoords=2):
    header = """pull = true
free-energy = no
pull-print-COM = false
pull-print-ref-value = false
pull-print-components = false
pull-xout-average = false
pull-nstxout = 50
dt = 0.002
nsteps = 1000000
ref-t: 300
"""
    header += f"pull-ncoords = {ncoords}\n"
    for i in range(ncoords):
        header += f"""pull-coord {i}:
type = umbrella
geometry = dihedral
start = false
init = {center if i == 0 else 0}
rate = 0
k = {k if i == 0 else psi_k}
kB = {k if i == 0 else psi_k}
"""
    return header


def window():
    return {"center_degrees": -180, "force_constant_kj_mol_rad2": 200,
            "expected_dt_ps": 0.1, "production_start_ps": 0, "production_end_ps": 2000}


def test_half_open_circle_and_nearest_image():
    assert np.array_equal(wham.wrap_degrees([-540, -180, 180, 540, 0]), [-180, -180, -180, -180, 0])
    assert wham.harmonic_bias(179, -179, 200) == pytest.approx(0.5 * 200 * np.deg2rad(2)**2)


def test_radian_force_constant_is_not_degree_force_constant():
    bias = wham.harmonic_bias(10, 0, 200)
    assert bias == pytest.approx(3.0461741978670855)
    k_degrees = 200 * (np.pi / 180)**2
    assert 0.5 * k_degrees * 10**2 == pytest.approx(bias)
    assert 0.5 * 200 * 10**2 / bias == pytest.approx((180 / np.pi)**2)


def test_periodic_equivariance():
    angles = np.linspace(-180, 180, 361)
    assert np.allclose(wham.harmonic_bias(angles + 360, 170, 200), wham.harmonic_bias(angles, -190, 200))


def test_select_exact_production_excludes_zero():
    data = np.column_stack([np.arange(11) / 10, np.linspace(-180, 180, 11), np.zeros(11)])
    selected = wham.select_production(data, 0, 1, 0.1)
    assert selected.shape == (10, 3)
    assert selected[0, 0] == pytest.approx(0.1)
    assert selected[-1, 0] == pytest.approx(1)


@pytest.mark.parametrize("kind", ["gap", "duplicate", "wrong_cadence", "out_of_range"])
def test_incomplete_or_invalid_observations_rejected(kind):
    data = np.column_stack([np.arange(11) / 10, np.zeros(11)])
    if kind == "gap":
        data = np.delete(data, 3, axis=0)
    elif kind == "duplicate":
        data[3, 0] = data[2, 0]
    elif kind == "wrong_cadence":
        data[:, 0] *= 2
    else:
        data[2, 1] = 181
    with pytest.raises(ValueError):
        wham.select_production(data, 0, 1, 0.1)


def test_xvg_failures_are_not_silent_drops(tmp_path):
    path = tmp_path / "x.xvg"
    for text in ["# empty\n", "0 1\n1 nan\n", "0 1\n1 2 3\n", "0 1\n&\n1 2\n"]:
        path.write_text(text)
        with pytest.raises(ValueError):
            wham.read_xvg(path)
    path.write_text('@ s0 legend "phi"\n# native output\n0 -180\n0.1 179\n')
    assert np.array_equal(wham.read_xvg(path), [[0, -180], [0.1, 179]])


def test_empty_probability_stays_missing():
    native = np.column_stack([[-135, -45, 45, 135], [0.0, 1.0, 2.0, 0.0]])
    _, density, pmf = wham.normalize_profile(native, 300, 1)
    assert density.sum() * 90 == pytest.approx(1)
    assert np.isnan(pmf[[0, 3]]).all()
    assert pmf[1] == 0
    assert pmf[2] == pytest.approx(-wham.R_KJ * 300 * np.log(2))
    with pytest.raises(ValueError, match="Reference bin unsampled"):
        wham.normalize_profile(native, 300, 0)


def test_wrong_periodic_grid_rejected():
    with pytest.raises(ValueError, match="angular grid"):
        wham.normalize_profile(np.array([[0, 1], [1, 1], [2, 1]]), 300, 0)


def test_overlap_includes_periodic_neighbor_and_disconnect():
    angles = [np.array([-179, 179]), np.array([0, 1]), np.array([179, 181])]
    _, matrix, report = wham.histogram_diagnostics(angles, [-165, 0, 165], 180)
    assert matrix[0, 2] == pytest.approx(1)
    assert report["adjacent_windows_including_seam"][-1]["crosses_periodic_seam"]
    assert report["adjacent_windows_including_seam"][-1]["overlap"] == pytest.approx(1)
    assert not report["observed_support_graph_connected"]
    assert np.allclose(matrix, matrix.T)


def test_temporal_block_bootstrap_is_exact_length_and_reproducible():
    first, starts = wham.block_resample_indices(103, 12, np.random.default_rng(20260924))
    second, starts2 = wham.block_resample_indices(103, 12, np.random.default_rng(20260924))
    assert len(first) == 103
    assert np.array_equal(first, second) and np.array_equal(starts, starts2)
    assert np.array_equal(first, ((starts[:, None] + np.arange(12)) % 103).ravel()[:103])
    assert first.min() >= 0 and first.max() < 103
    with pytest.raises(ValueError):
        wham.block_resample_indices(103, 104, np.random.default_rng(1))


def test_autocorrelation_uses_periodic_observables():
    angles = np.tile([179, -179], 500)
    result = wham.angular_mixing(angles, 0.1)
    assert set(result["periodic_observables"]) == {"cos", "sin", "positive_half_circle"}
    assert result["first_second_half_histogram_total_variation"] == 0
    assert result["periodic_observables"]["cos"]["g"] is None


def test_autocorrelation_detects_correlated_chain():
    rng = np.random.default_rng(72)
    innovations = rng.normal(size=50000)
    chain = np.zeros(50000)
    for i in range(1, len(chain)):
        chain[i] = 0.9 * chain[i-1] + innovations[i]
    measured = wham.statistical_inefficiency(chain)["g"]
    assert 12 < measured < 28  # theoretical g=(1+.9)/(1-.9)=19
    assert wham.statistical_inefficiency(np.ones(100))["g"] is None


def test_audit_actual_tpr_includes_unbiased_psi():
    audit = wham.audit_tpr_dump(native_dump(), window(), 300)
    assert audit["selection"] == [1, 0]
    assert audit["psi_unbiased"]
    assert audit["output_dt_ps"] == pytest.approx(0.1)


@pytest.mark.parametrize("before,after", [
    ("k = 200", "k = 0.060923"), ("init = -180", "init = -165"),
    ("rate = 0", "rate = 1"), ("geometry = dihedral", "geometry = distance"),
    ("ref-t: 300", "ref-t: 310"), ("pull-xout-average = false", "pull-xout-average = true"),
    ("pull-print-ref-value = false", "pull-print-ref-value = true"),
    ("nsteps = 1000000", "nsteps = 10000"), ("free-energy = no", "free-energy = yes"),
])
def test_tpr_semantic_mismatch_rejected(before, after):
    with pytest.raises(ValueError):
        wham.audit_tpr_dump(native_dump().replace(before, after), window(), 300)


def test_cannot_ignore_biased_psi():
    with pytest.raises(ValueError, match="Psi monitor is biased"):
        wham.audit_tpr_dump(native_dump(psi_k=1), window(), 300)


def test_native_fixed_bounds_omit_both_auto_forms(tmp_path, monkeypatch):
    runner = wham.NativeGromacs(tmp_path, image="")
    captured = []
    def fake_run(args, directory, name):
        captured.extend(map(str, args))
        np.savetxt(directory / "probability.xvg", [[-90, 1], [90, 1]])
        return "Converged in 2 iterations"
    monkeypatch.setattr(runner, "run", fake_run)
    output = runner.wham([tmp_path / "native.tpr"], [np.array([[0, 179], [1, -179]])],
                         tmp_path / "profile", bins=2, temperature=300, selections=[[1]])
    assert "-auto" not in captured and "-noauto" not in captured
    assert "-cycl" in captured and "-nolog" in captured and "-is" in captured
    assert output.shape == (2, 2)
    assert not list(tmp_path.glob("wham-pullx-*"))
    derived = json.loads((tmp_path / "profile" / "derived-inputs.json").read_text())
    assert derived["files"][0]["samples"] == 2


def test_deterministic_synthetic_data_are_periodic_and_count_preserving():
    potential = lambda x: 3 * (1 - np.cos(np.deg2rad(x)))
    first = wham.synthetic_observations([-180, 0], 200, 300, 180, 10000, potential)
    second = wham.synthetic_observations([180, 0], 200, 300, 180, 10000, potential)
    assert all(a.shape == (10000, 2) for a in first)
    assert all(np.array_equal(a, b) for a, b in zip(first, second))


def test_json_never_emits_nonstandard_nan():
    assert wham.clean_json({"array": np.array([np.nan, np.inf, 1.0])}) == {"array": [None, None, 1.0]}


def test_unbiased_overlay_preserves_unsampled_bins(tmp_path):
    path = tmp_path / "frames.csv"
    path.write_text("production_time_ps,phi_degrees\n" + "".join(f"{i},-59\n" for i in range(1001)))
    density, pmf, receipt = wham.load_unbiased(path, 180, 300, 60)
    assert receipt["frames"] == 1000
    assert density.sum() * 2 == pytest.approx(1)
    assert np.count_nonzero(np.isfinite(pmf)) == 1
    assert len(receipt["empty_bin_indices"]) == 179


def test_cli_plumbing_with_mocked_native_is_not_scientific_evidence(tmp_path, monkeypatch):
    """Exercise complete file/report flow; only real native tests qualify WHAM."""
    source = tmp_path / "unit-test-mock-inputs"
    source.mkdir()
    windows = []
    times = np.arange(20001) / 10
    phi = (np.arange(20001) * 137) % 360 - 180
    np.savetxt(source / "mock-pullx.xvg", np.column_stack([times, phi, np.zeros(len(phi))]), fmt="%.4f")
    for i in range(24):
        (source / f"window-{i:02d}.tpr").write_text("MOCK, NOT A NATIVE TPR")
        windows.append({"id": f"unit-test-{i}", "operation_id": "MOCK_ONLY",
                        "status": "succeeded", "tpr": f"window-{i:02d}.tpr", "pullx": "mock-pullx.xvg",
                        "center_degrees": -180 + 15*i, "force_constant_kj_mol_rad2": 200,
                        "production_start_ps": 0, "production_end_ps": 2000, "expected_dt_ps": 0.1})
    (source / "mock-unbiased.csv").write_text("production_time_ps,phi_degrees\n" +
                                             "".join(f"{i},-59\n" for i in range(1001)))
    manifest = source / "manifest.json"
    manifest.write_text(json.dumps({"evidence_kind": "real-native-md", "temperature_k": 300,
                                    "windows": windows, "unbiased": {"frames_csv": "mock-unbiased.csv"}}))
    monkeypatch.setattr(wham.NativeGromacs, "version", lambda self: {"version": "MOCK_NOT_NATIVE"})
    def fake_dump(self, args, directory, name):
        i = int(Path(args[-1]).stem.split("-")[-1])
        return native_dump(center=-180 + 15*i)
    monkeypatch.setattr(wham.NativeGromacs, "run", fake_dump)
    monkeypatch.setattr(wham.NativeGromacs, "wham", lambda self, *args, **kwargs:
                        np.column_stack([-179 + np.arange(180) * 2, np.ones(180)]))
    output = tmp_path / "unit-test-mock-output"
    args = SimpleNamespace(output=output, manifest=manifest, image="", gmx="MOCK",
                           bins=180, bootstrap=2, block_ps=100, seed=72, reference_deg=-60)
    assert wham.analyze(args) == 0
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["native"]["version"] == "MOCK_NOT_NATIVE"
    assert receipt["inputs_unchanged"] and receipt["scientific_convergence"] == "not-established"
    assert not receipt["customer_ready"]
    assert receipt["bootstrap"]["replicates_requested"] == 2
    assert len(receipt["psi_summary"]["windows_without_observed_half_circle_crossings"]) == 24
    assert (output / "phi-time-split.png").is_file()
    assert (output / "orthogonal-psi-mixing.png").is_file()
