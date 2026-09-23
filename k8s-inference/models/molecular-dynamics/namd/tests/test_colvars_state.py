import pytest

from fs2_namd.colvars_state import metadynamics, prepare, verify


HILL = "hill {\nstep 1000\nweight 0.01\ncenters 5.123\nwidths 0.4\n}\n"


def state(hills=HILL, extra="", grids=""):
    return ("configuration {\n step 1000\n version 2024-06-04\n}\n"
            "metadynamics {\n configuration {\n step 1000\n name radius_meta\n" + extra + "}\n" + grids + hills + "}\n")


def test_ungridded_marker_repair_preserves_original_and_hills(tmp_path):
    original, derived = tmp_path / "original.state", tmp_path / "derived.state"
    original.write_text(state())
    receipt = prepare(original, derived)
    assert original.read_text() == state()
    assert receipt["biases_repaired"] == ["radius_meta"]
    assert receipt["original_sha256"] != receipt["loaded_input_sha256"]
    assert metadynamics(derived.read_text())["radius_meta"]["keeps_hills"]
    assert verify(original, derived, 1000)["explicit_hills_restored"] == {"radius_meta": 1}


@pytest.mark.parametrize("value", [state(hills=""), state(extra="keepHills on\n"), state(grids="hills_energy { 1 2 3 }\n")])
def test_empty_already_marked_and_gridded_states_are_not_rewritten(tmp_path, value):
    original, derived = tmp_path / "original.state", tmp_path / "derived.state"
    original.write_text(value)
    assert prepare(original, derived)["repair"] is None
    assert not derived.exists()


def test_round_trip_detects_native_hill_loss_and_value_change(tmp_path):
    original, loaded = tmp_path / "original.state", tmp_path / "loaded.state"
    original.write_text(state())
    loaded.write_text(state(hills=""))
    with pytest.raises(ValueError, match="lost explicit"):
        verify(original, loaded, 1000)
    loaded.write_text(state(hills=HILL.replace("0.01", "0.02")))
    with pytest.raises(ValueError, match="changed explicit"):
        verify(original, loaded, 1000)


@pytest.mark.parametrize("value", [state(extra="keepHills off\n"), state(hills=HILL.replace("weight", "unknownWeight")), state(hills=HILL.replace("0.01", "nan")), state().replace("2024-06-04", "1900-01-01")])
def test_unsupported_explicit_override_and_hill_formats_fail(tmp_path, value):
    original, derived = tmp_path / "original.state", tmp_path / "derived.state"
    original.write_text(value)
    with pytest.raises(ValueError):
        prepare(original, derived)
