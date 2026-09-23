"""Convert the immutable AMBER master; validation is a separate required gate."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil

import parmed
import intermol.gromacs
import intermol.lammps


def convert(master, output):
    output.mkdir(parents=True, exist_ok=False)
    gromacs = output / "gromacs"
    lammps = output / "lammps"
    gromacs.mkdir()
    lammps.mkdir()
    topology = parmed.load_file(str(master / "system.prmtop"), xyz=str(master / "system.rst7"))
    topology.save(str(gromacs / "system.top"))
    topology.save(str(gromacs / "system.gro"), precision=8)
    # ParmEd's coordinate precision argument does not change its five-decimal
    # box writer. Retain the canonical cell instead of silently rounding it.
    gro_lines = (gromacs / "system.gro").read_text().splitlines()
    gro_lines[-1] = " ".join(f"{length / 10:.10f}" for length in topology.box[:3])
    (gromacs / "system.gro").write_text("\n".join(gro_lines) + "\n")
    # Use the native flexible branch here so no bonded parameter is discarded
    # by treating water as already constrained. Dynamics constraints are added
    # explicitly in each engine's separate native protocol.
    system = intermol.gromacs.load(str(gromacs / "system.top"), str(gromacs / "system.gro"), defines={"FLEXIBLE": True})
    intermol.lammps.save(str(lammps / "system.input"), system,
                        nonbonded_style="pair_style lj/cut/coul/long 10.0 10.0\nkspace_style pppm 1e-6\npair_modify shift no tail yes\n")
    shutil.copyfile(master / "protocol.json", output / "protocol.json")
    receipt = {"status": "converted-unvalidated", "master_manifest_sha256": hashlib.sha256((master / "master-manifest.json").read_bytes()).hexdigest(),
               "parmed_version": parmed.__version__,
               "intermol_commit": "7125764d42f6e6c589dc2d1df71e3e812b3a7b27",
               "intermol_patch": "Python3.12 Versioneer only: SafeConfigParser->ConfigParser,readfp->read_file; no conversion physics changed",
               "methods": ["ParmEd AMBER to GROMACS,8decimal-place nm coordinates and explicit10decimal canonical nm cell", "InterMol GROMACS(FLEXIBLE) to LAMMPS"],
               "files": [{"path": str(path.relative_to(output)), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                         for path in sorted(output.rglob("*")) if path.is_file()]}
    (output / "conversion.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(convert(args.master, args.output), indent=2))
