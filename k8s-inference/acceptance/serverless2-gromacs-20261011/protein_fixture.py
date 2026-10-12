"""Pinned public benchMEM input; never reads customer simulation data.

Download https://www.mpinat.mpg.de/benchMEM and extract benchMEM.tpr outside
Git. The upstream CC-BY-4.0 input is unmodified; convert-tpr changes only the
finite run length. Its disabled energy/trajectory output remains disabled.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import math
import tarfile
from pathlib import Path

TPR_SHA256 = "5099268bf3a3d948c03b3b78432f29a2c5d4207b54d02e7e294f8138b587d473"
PROVENANCE = {
    "fixture": "mpinat-benchMEM",
    "source": "https://www.mpinat.mpg.de/grubmueller/bench",
    "download": "https://www.mpinat.mpg.de/benchMEM",
    "archive_sha256": "3c1c8cd4f274d532f48c4668e1490d389486850d6b3b258dfad4581aa11380a4",
    "tpr_sha256": TPR_SHA256,
    "license": "CC-BY-4.0",
    "attribution": "Dept. of Theoretical and Computational Biophysics, "
    "Max Planck Institute for Multidisciplinary Sciences, Göttingen",
    "atoms": 81743,
    "dt_ps": 0.002,
    "electrostatics": "PME",
    "cutoff_nm": 1.0,
    "pme_grid": [96, 96, 80],
    "pme_order": 4,
    "constraints": "all-bonds; CPU update required",
    "temperature_K": 300,
    "thermostat": "Berendsen",
    "barostat": "Berendsen, isotropic, 1 bar",
    "seed_in_tpr": 1993,
    "velocities": "from original public TPR; not regenerated",
    "trajectory_output": False,
    "energy_output": False,
    "scope": "protein/PME execution and recovery, not ensemble convergence",
}


def fixture(source: Path, steps: int) -> tuple[bytes, dict, dict]:
    if isinstance(steps, bool) or not isinstance(steps, int) or not 10000 <= steps <= 2000000:
        raise ValueError("protein acceptance requires 10,000 to 2,000,000 finite steps")
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != TPR_SHA256:
        raise ValueError("benchMEM.tpr does not match the pinned public input")
    target = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        entry = tarfile.TarInfo("benchMEM.tpr")
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    parameters = {
        "schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
        "threads": 6,
        "segment_minutes": 1.0,
        "checkpoint_minutes": 0.5,
        "max_wall_seconds": 3600,
        "jobs": [
            {
                "id": "benchmem",
                "steps": [
                    {
                        "id": "extend",
                        "command": "convert-tpr",
                        "args": ["-s", "benchMEM.tpr", "-o", "run.tpr", "-nsteps", str(steps)],
                        "expected_outputs": ["run.tpr"],
                    },
                    {
                        "id": "md",
                        "command": "mdrun",
                        "args": [
                            "-s",
                            "run.tpr",
                            "-deffnm",
                            "md",
                            "-nb",
                            "gpu",
                            "-pme",
                            "gpu",
                            "-update",
                            "cpu",
                        ],
                        "expected_outputs": ["md.gro"],
                    },
                ],
            }
        ],
    }
    expected = {
        "completed_steps": ["extend", "md"],
        "command_counts": {"convert-tpr": 1, "grompp": 0},
        "protocol": {**PROVENANCE, "requested_steps": steps, "duration_ps": steps * 0.002},
    }
    return target.getvalue(), parameters, expected


def validate_native(result: dict, receipt: dict) -> dict:
    expected = receipt.get(
        "expected",
        {
            "completed_steps": ["prepare", "md"],
            "command_counts": {"grompp": 1},
        },
    )
    if result["completed_steps"] != expected["completed_steps"]:
        raise AssertionError("workflow did not complete every expected step")
    for command, count in expected["command_counts"].items():
        if sum(c["command"][1] == command for c in result["commands"]) != count:
            raise AssertionError("preparation was missing or repeated after recovery")
    md = [c for c in result["commands"] if c["command"][1] == "mdrun"]
    if not md or md[-1].get("checkpoint_step") != receipt["steps"]:
        raise AssertionError("native checkpoint does not confirm requested completion")
    if any(c["exit_code"] != 0 for c in result["commands"]):
        raise AssertionError("native result contains a failed command")
    return {
        "final_step": md[-1]["checkpoint_step"],
        "md_segments": len(md),
        "segment_ns_per_day": [c.get("performance_ns_per_day") for c in md],
        "native_wall_seconds": sum(c["wall_seconds"] for c in result["commands"]),
    }


def validate_coordinates(data: bytes, atoms: int = 81743) -> dict:
    """Output integrity, not thermodynamic/biological validity or convergence."""
    lines = data.decode().splitlines()
    if int(lines[1]) != atoms or len(lines) != atoms + 3:
        raise AssertionError("final GRO has wrong particle count or is truncated")
    for line in lines[2:-1]:
        if not all(math.isfinite(float(line[start : start + 8])) for start in (20, 28, 36)):
            raise AssertionError("final GRO has non-finite coordinates")
    box = [float(value) for value in lines[-1].split()]
    if len(box) not in (3, 9) or not all(math.isfinite(v) for v in box) or min(box[:3]) <= 0:
        raise AssertionError("final GRO has an invalid periodic box")
    return {"particle_count": atoms, "finite_coordinates": True, "box_nm": box}
