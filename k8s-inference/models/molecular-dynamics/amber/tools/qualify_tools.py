"""Validate native AMBER preparation and topology/coordinate analysis locally.

Run with the bundled AmberTools Python inside the exact composed engine image.
This is preparation/analysis evidence, not sustained MD or hosted qualification.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import numpy as np
import parmed
from scipy.io import netcdf_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    for path in (Path(__file__).parent / "fixtures").iterdir():
        shutil.copyfile(path, args.output / path.name)
    env = {**os.environ, "AMBERHOME": "/opt/ambertools",
           "PATH": "/opt/ambertools/bin:/usr/bin:/bin",
           "LD_LIBRARY_PATH": "/opt/ambertools/lib", "OMP_NUM_THREADS": "1"}
    commands = [
        ["/opt/ambertools/bin/tleap", "-f", "alanine.leap"],
        ["/opt/ambertools/bin/parmed", "-p", "alanine-opc.prmtop", "-c", "alanine-opc.inpcrd", "-i", "inspect.parmed"],
        ["/opt/ambertools/bin/cpptraj", "-i", "inspect.cpptraj"],
    ]
    timings = []
    for index, command in enumerate(commands):
        start = time.monotonic()
        with (args.output / f"tool-{index}.log").open("wb") as log:
            result = subprocess.run(command, cwd=args.output, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=300, check=False)
        timings.append({"argv": command, "exit_code": result.returncode, "seconds": time.monotonic() - start})
        if result.returncode:
            raise RuntimeError(f"Native preparation/analysis failed; inspect tool-{index}.log")
    systems = {}
    for name in ("alanine-vacuum", "alanine-opc"):
        topology = parmed.load_file(str(args.output / f"{name}.prmtop"),
                                   xyz=str(args.output / f"{name}.inpcrd"))
        if not np.isfinite(topology.coordinates).all() or abs(sum(atom.charge for atom in topology.atoms)) > 1e-5:
            raise ValueError("Prepared coordinates/charge are invalid")
        systems[name] = {"atoms": len(topology.atoms), "residues": len(topology.residues),
                         "total_charge": sum(atom.charge for atom in topology.atoms)}
    if systems["alanine-vacuum"]["atoms"] != 22 or systems["alanine-vacuum"]["residues"] != 3:
        raise ValueError("Prepared peptide is not the requested ACE-ALA-NME system")
    if systems["alanine-opc"]["atoms"] <= 1000 or systems["alanine-opc"]["residues"] <= 250:
        raise ValueError("Explicit OPC solvation did not produce the intended water box")
    with netcdf_file(str(args.output / "prepared.nc"), "r", mmap=False) as trajectory:
        coordinates = trajectory.variables["coordinates"][:].copy()
    if coordinates.shape != (1, systems["alanine-opc"]["atoms"], 3) or not np.isfinite(coordinates).all():
        raise ValueError("CPPTRAJ's coordinate round trip is incomplete")
    files = [{"path": path.name, "bytes": path.stat().st_size,
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
             for path in sorted(args.output.iterdir()) if path.is_file()]
    receipt = {"status": "passed", "scope": "native-preparation-and-coordinate-analysis",
               "systems": systems, "commands": timings, "files": files,
               "sustained_md_tested": False, "hosted_tested": False, "gpu_snapshot_tested": False}
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({key: value for key, value in receipt.items() if key not in {"commands", "files"}}))


if __name__ == "__main__":
    main()
