"""Synthetic negative controls for the native umbrella observation checks."""

import numpy as np
import pytest
from md_analyze import validate_umbrella_observations


def fixture(tmp_path):
    times = np.arange(21) * 0.1
    pull = np.column_stack([times, -170 + np.sin(times), 100 + np.cos(times)])
    np.savetxt(tmp_path / "production.part0001_pullx.xvg", pull)
    rows = [
        {"time_ps": row[0], "phi_degrees": row[1], "psi_degrees": row[2]}
        for row in pull[::10]
    ]
    return pull, rows


def test_native_phi_psi_match_saved_coordinates(tmp_path):
    _, rows = fixture(tmp_path)
    proof = validate_umbrella_observations(tmp_path, rows, 2)
    assert proof["native_pull_samples"] == 21
    assert proof["maximum_periodic_error_degrees"] < 1e-9
    assert not proof["compiled_bias_or_global_pmf_validated"]


def test_swapped_native_cv_columns_do_not_pass(tmp_path):
    pull, rows = fixture(tmp_path)
    np.savetxt(tmp_path / "production.part0001_pullx.xvg", pull[:, [0, 2, 1]])
    with pytest.raises(ValueError, match="coordinate_pull_mismatch"):
        validate_umbrella_observations(tmp_path, rows, 2)


def test_missing_native_samples_do_not_pass(tmp_path):
    pull, rows = fixture(tmp_path)
    np.savetxt(tmp_path / "production.part0001_pullx.xvg", np.delete(pull, 5, axis=0))
    with pytest.raises(ValueError, match="schedule_or_units"):
        validate_umbrella_observations(tmp_path, rows, 2)
