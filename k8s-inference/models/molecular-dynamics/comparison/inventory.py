"""Record available programs/libraries without executing a simulation."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import sys


def inspect(scope):
    programs = ["gmx", "gmx_mpi", "namd3", "pmemd", "pmemd.cuda", "lmp",
                "tleap", "parmed", "cpptraj", "intermol-convert", "ffmpeg",
                "ffprobe", "blender", "vmd", "pymol"]
    libraries = {}
    for name in ["ParmEd", "InterMol", "OpenMM", "MDAnalysis", "numpy", "scipy",
                 "matplotlib", "Pillow", "pyvista", "vtk"]:
        try:
            libraries[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            libraries[name] = None
    force_fields = {}
    for name in ["leaprc.protein.ff14SB", "leaprc.water.tip3p"]:
        path = Path("/opt/ambertools/dat/leap/cmd") / name
        force_fields[name] = ({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                              if path.is_file() else None)
    return {"at": datetime.now(timezone.utc).isoformat(), "scope": scope,
            "host": platform.platform(), "python": sys.version,
            "programs": {name: shutil.which(name) for name in programs},
            "libraries": libraries, "force_fields": force_fields,
            "simulation_executed": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = inspect(args.scope)
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
