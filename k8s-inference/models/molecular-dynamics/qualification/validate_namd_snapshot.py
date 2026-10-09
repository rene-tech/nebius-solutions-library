"""Validate the exact public ApoA1 continuation used by the snapshot probe."""

import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--native-validator-directory", type=Path, default=Path("/checkpoints/validation"))
    args = parser.parse_args()
    sys.path.insert(0, str(args.native_validator_directory))
    from validate_namd import binary_vectors, dcd, energy_drift, xsc_step

    work = args.directory / "work"
    log = args.directory / "worker.log"
    result = {
        "coordinates_atoms": binary_vectors(work / "production.part000001.coor"),
        "velocities_atoms": binary_vectors(work / "production.part000001.vel"),
        "checkpoint_step": xsc_step(work / "production.part000001.xsc"),
        "trajectory": dcd(work / "production.part000001.dcd"),
        "finite_observables": energy_drift([log]),
        "native_success_marker": "FS2_SEGMENT_COMPLETE 121000" in log.read_text(),
        "native_end_marker": "End of program" in log.read_text(),
    }
    expected = {"atoms": 92224, "frames": 10, "first_step": 30000, "interval_steps": 10000, "last_step": 120000}
    result["passed"] = (
        result["coordinates_atoms"] == result["velocities_atoms"] == 92224
        and result["checkpoint_step"] == 121000 and result["trajectory"] == expected
        and result["native_success_marker"] and result["native_end_marker"]
        and result["finite_observables"]["max_relative_total_energy_deviation"] <= 0.005
    )
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
