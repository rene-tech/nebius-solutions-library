import pytest

from validate_gromacs_fixed_pme import energy_series, fixed_command, native_parameters


def native_log(stage="production"):
    values = {"fourier-nx": 64, "fourier-ny": 64, "fourier-nz": 64, "pme-order": 4,
              "rcoulomb": 1, "rvdw": 1, "ewald-rtol": 1e-5, "nsteps": 500000,
              "nstlist": 10, "rlist": 1.15, "dt": .002, "nstxout-compressed": 500,
              "nstenergy": 500, "lincs-order": 8, "lincs-iter": 2, "ld-seed": 25,
              "coulombtype": "PME", "coulomb-modifier": "None", "vdw-modifier": "None",
              "DispCorr": "EnerPres", "integrator": "sd", "constraint-algorithm": "Lincs",
              "pcoupl": "C-rescale"}
    if stage == "minimize":
        values.update(nsteps=5000, integrator="steep")
    return ("Command line:\n gmx mdrun -s input.tpr -notunepme\n" +
            "\n".join(f"  {k} = {v}" for k, v in values.items()) +
            "\n ref-t: 300\n tau-t: 1\nPME tasks will do all aspects on the GPU\n" +
            "Performance: 700.123 0.03\nFinished mdrun on rank 0\n")


def test_explicit_fixed_pme_allows_native_completion():
    assert native_parameters(native_log(), "production", 25)["autotuning_events"] == 0
    assert native_parameters(native_log("minimize"), "minimize")["gpu_pme"] is False


@pytest.mark.parametrize("old,new", [
    ("fourier-nx = 64", "fourier-nx = 48"), ("fourier-ny = 64", "fourier-ny = 60"),
    ("fourier-nz = 64", "fourier-nz = 44"), ("pme-order = 4", "pme-order = 6"),
    ("rcoulomb = 1", "rcoulomb = 1.333"), ("rvdw = 1", "rvdw = 1.2"),
    ("ewald-rtol = 1e-05", "ewald-rtol = 0.001"), ("ld-seed = 25", "ld-seed = 26"),
    ("lincs-order = 8", "lincs-order = 4"), ("nsteps = 500000", "nsteps = 1000"),
    ("ref-t: 300", "ref-t: 310"), ("tau-t: 1", "tau-t: 2"),
    ("DispCorr = EnerPres", "DispCorr = No"), ("Finished mdrun", "Incomplete mdrun"),
])
def test_rejects_scientific_parameter_or_completion_change(old, new):
    assert old in native_log()
    with pytest.raises(ValueError):
        native_parameters(native_log().replace(old, new), "production", 25)


@pytest.mark.parametrize("event", [
    "step 22360: timed with pme grid 48 48 48, coulomb cutoff 1.333",
    "PP/PME load balancing changed the cut-off and PME settings:",
    "optimal pme grid 64 64 64", "LINCS WARNING", "Fatal error:",
    "  rcoulomb = 1.333\n  rcoulomb = 1",
])
def test_rejects_transient_tuning_even_when_final_settings_are_correct(event):
    with pytest.raises(ValueError):
        native_parameters(native_log() + event, "production", 25)


@pytest.mark.parametrize("flags", [[], ["-tunepme"], ["-notunepme", "-tunepme"], ["-notunepme", "-notunepme"]])
def test_requires_one_unambiguous_negative_tuning_flag(flags):
    with pytest.raises(ValueError, match="disable PME tuning"):
        fixed_command(["gmx", "mdrun", *flags])


def xvg(path, density):
    labels = ["Potential", "Total Energy", "Temperature", "Pressure"] + (["Density"] if density else [])
    path.write_text("\n".join(f'@ s{i} legend "{name}"' for i, name in enumerate(labels)) + "\n" +
                    "\n".join(f"{i} -4.184 0 300 {i}" + (" 1000" if density else "") for i in range(4)))


@pytest.mark.parametrize("density", [True, False])
def test_native_npt_density_and_explicit_nvt_absence(tmp_path, density):
    path = tmp_path / "energy.xvg"
    xvg(path, density)
    out = energy_series(path, 3, density=density)
    assert out["rows"] == 4 and out["pressure_mean_bar"] == 2
    assert out["potential_mean_kcal_mol"] == -1
    assert out["density_mean_g_cm3"] == (1 if density else None)


@pytest.mark.parametrize("old,new", [("300 2", "nan 2"), ("2 -4.184", "1 -4.184"), ('"Pressure"', '"Density"')])
def test_rejects_nonfinite_or_ambiguous_thermodynamics(tmp_path, old, new):
    path = tmp_path / "energy.xvg"
    xvg(path, True)
    path.write_text(path.read_text().replace(old, new))
    with pytest.raises(ValueError):
        energy_series(path, 3)
