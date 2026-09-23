import math
import struct

import pytest

from make_fixture import configuration
from validate_campaign import dcd, radius_metadynamics


def test_fixture_seed_schedule_is_explicit_native_input():
    source = "seed 123\nset doRestart 0\nrun 100\n"
    managed = configuration(source, managed=True, ensemble="npt", gpu_mode="resident", seed=314159)
    native = configuration(source, managed=False, ensemble="npt", gpu_mode="resident", seed=314159)
    assert "seed [expr {314159 + $fs2_first_step}]" in managed
    assert "seed 314159" in native


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
