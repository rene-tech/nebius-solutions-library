import hashlib
import math
import struct

import pytest

from audit_binary import summarize
from make_fixture import colvars_configuration, configuration
from validate_campaign import dcd, production_timing, radius_metadynamics, verify_grid_round_trip


def test_binary_inventory_does_not_infer_runtime_dispatch_from_sm89_library_code():
    result = summarize("ELF file 1: n.1.sm_86.cubin\nELF file 2: n.2.sm_89.cubin\n",
                       "PTX file 1: n.1.sm_120.ptx\n",
                       {"sm_86": {"stdout": "STT_FUNC STB_GLOBAL STO_ENTRY _ZbondedForcesKernel\n"},
                        "sm_89": {"stdout": "STT_FUNC STB_GLOBAL STO_ENTRY _ZcurandState\n"}})
    assert result["elf_architecture_counts"] == {"sm_86": 1, "sm_89": 1}
    assert result["ptx_target_counts"] == {"sm_120": 1}
    assert result["selected_architecture_symbol_inventory"]["sm_89"]["namd_marker_entry_count"] == 0
    assert result["selected_architecture_symbol_inventory"]["sm_86"]["namd_marker_entry_count"] == 1
    assert result["runtime_dispatch_proven"] is False


def test_fixture_seed_schedule_is_explicit_native_input():
    source = "seed 123\nset doRestart 0\nrun 100\n"
    managed = configuration(source, managed=True, ensemble="npt", gpu_mode="resident", seed=314159)
    native = configuration(source, managed=False, ensemble="npt", gpu_mode="resident", seed=314159)
    assert "seed [expr {314159 + $fs2_first_step}]" in managed
    assert "seed 314159" in native


def test_grid_protocol_is_explicit_and_original_ungridded_bytes_are_unchanged():
    ungridded = colvars_configuration()
    assert hashlib.sha256(ungridded.encode()).hexdigest() == "b797f28ad70130235e6ec461ccd8f99deee98b3717a0a922a7703de4fd866927"
    grid = colvars_configuration(grid=True)
    assert "useGrids on" in grid and "keepHills on" in grid
    assert "lowerBoundary 0.0" in grid and "upperBoundary 20.0" in grid
    assert "writeFreeEnergyFile on" in grid


def test_production_process_rate_uses_actual_steps_and_wall_time():
    commands = [{"configured_first_step": n * 100000, "checkpoint_step": (n + 1) * 100000,
                 "timestep_fs": 2.0, "wall_seconds": 60.0, "cpu_user_seconds": 240.0} for n in (0, 1)]
    timing = production_timing(commands)
    assert timing["production_simulated_ns"] == pytest.approx(0.4)
    assert timing["production_process_ns_per_day"] == pytest.approx(288.0)
    assert timing["cpu_user_cores_during_native_production"] == pytest.approx(4.0)
    with pytest.raises(ValueError, match="positive"):
        production_timing([])


def test_native_grid_round_trip_checks_every_value_and_metadata():
    original = "hills_energy {\n grid { lower 0 width 0.2 bins 2 }\n 1.0 2.0\n}\nhills_energy_gradients {\n 0.3 0.4\n}\n"
    assert verify_grid_round_trip(original, original)["hills_energy"]["numeric_values_verified"] == 5
    with pytest.raises(ValueError, match="grid values"):
        verify_grid_round_trip(original, original.replace("0.4", "0.5"))
    with pytest.raises(ValueError, match="grid values"):
        verify_grid_round_trip(original, original.replace("0.4", "nan"))
    with pytest.raises(ValueError, match="grid metadata"):
        verify_grid_round_trip(original, original.replace("lower", "upper"))
    with pytest.raises(ValueError, match="lost"):
        verify_grid_round_trip(original, original.replace("hills_energy_gradients", "missing"))


def test_dcd_reads_all_frames_and_rejects_nan(tmp_path):
    def record(value):
        return struct.pack("<i", len(value)) + value + struct.pack("<i", len(value))
    control = [0] * 20
    control[:3] = [2, 10, 10]
    prefix = record(b"CORD" + struct.pack("<20i", *control)) + record(b"test") + record(struct.pack("<i", 1))
    path = tmp_path / "test.dcd"
    path.write_bytes(prefix + b"".join(record(struct.pack("<f", v)) for v in [1, 2, 3, 4, 5, 6]))
    assert dcd(path) == {"atoms": 1, "frames": 2, "first_step": 10, "interval_steps": 10, "last_step": 20}
    path.write_bytes(prefix + b"".join(record(struct.pack("<f", v)) for v in [1, 2, 3, 4, 5, math.nan]))
    with pytest.raises(ValueError, match="non-finite"):
        dcd(path)


def test_bias_checkpoint_preserves_every_prior_hill(tmp_path):
    def hill(step, weight="0.01"):
        return "hill {\nstep " + str(step) + "\nweight " + weight + "\ncenters 5.0\nwidths 0.4\n}\n"
    for number in (1, 2):
        path = tmp_path / f"production.part{number:06d}.colvars.state"
        path.write_text("configuration { step " + str(number * 1000) + " }\n" + "".join(hill(n * 1000) for n in range(1, number + 1)))
        path.with_suffix(".traj").write_text(f"# step radius\n{number * 1000} 5.0\n")
    (tmp_path / "production.part000001.restart.colvars.state").write_text("older native periodic state")
    assert radius_metadynamics(tmp_path)["states"][-1]["hills"] == 2
    path.write_text("configuration { step 2000 }\n" + hill(1000, "0.02") + hill(2000))
    with pytest.raises(ValueError, match="lost or changed"):
        radius_metadynamics(tmp_path)
