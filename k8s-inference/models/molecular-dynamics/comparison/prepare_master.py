"""Prepare the single immutable ff14SB/TIP3P master with pinned AmberTools.

Run inside the AMBER engine image using /opt/ambertools/bin/python. This prepares
inputs only; it does not claim conversion equivalence or completed dynamics.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import numpy as np
import parmed

ENGINE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/amber26-engine@sha256:cea619dcb5f8a8577a17edd7ce0fdd70e6718838f9d5af47d7731ca8388fab48"

LEAP = """source leaprc.protein.ff14SB
source leaprc.water.tip3p
peptide = sequence { ACE ALA NME }
check peptide
saveAmberParm peptide peptide.prmtop peptide.rst7
savePdb peptide peptide.pdb
solvateBox peptide TIP3PBOX 15.0 iso
check peptide
saveAmberParm peptide leap-system.prmtop leap-system.rst7
savePdb peptide leap-system.pdb
quit
"""


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(output):
    output.mkdir(parents=True, exist_ok=False)
    (output / "prepare.leap").write_text(LEAP)
    env = {**os.environ, "AMBERHOME": "/opt/ambertools",
           "PATH": "/opt/ambertools/bin:/usr/bin:/bin",
           "LD_LIBRARY_PATH": "/opt/ambertools/lib", "OMP_NUM_THREADS": "1"}
    command = ["/opt/ambertools/bin/tleap", "-f", "prepare.leap"]
    with (output / "prepare.log").open("wb") as log:
        subprocess.run(command, cwd=output, env=env, stdout=log,
                       stderr=subprocess.STDOUT, check=True, timeout=300)
    top = parmed.load_file(str(output / "leap-system.prmtop"), xyz=str(output / "leap-system.rst7"))
    if [r.name for r in top.residues[:3]] != ["ACE", "ALA", "NME"]:
        raise ValueError("Native LEaP did not prepare ACE–ALA–NME")
    peptide_indices = [a.idx for a in top.atoms if a.residue.idx < 3]
    if len(peptide_indices) != 22:
        raise ValueError("Unexpected capped alanine atom count")
    if not np.allclose(top.box[3:], 90):
        raise ValueError("Expected an orthorhombic canonical box")
    box = np.array(top.box[:3])
    xyz = top.coordinates.copy()
    pep = xyz[peptide_indices]
    xyz += box / 2 - (pep.min(axis=0) + pep.max(axis=0)) / 2
    for residue in top.residues[3:]:
        if residue.name != "WAT" or len(residue.atoms) != 3:
            raise ValueError("Unexpected solvent: must be three-site TIP3P WAT")
        indices = [atom.idx for atom in residue.atoms]
        xyz[indices] -= box * np.floor(xyz[indices].mean(axis=0) / box)
    top.coordinates = xyz
    clearance = np.minimum(xyz[peptide_indices].min(axis=0), box - xyz[peptide_indices].max(axis=0))
    if clearance.min() < 10.0 or not np.isfinite(xyz).all():
        raise ValueError("Canonical system fails requested 1 nm clearance/finite coordinates")
    charge = sum(a.charge for a in top.atoms)
    if abs(charge) > 1e-6:
        raise ValueError("Unexpected nonneutral capped peptide + TIP3P system")
    top.save(str(output / "system.prmtop"))
    top.save(str(output / "system.rst7"))
    top.save(str(output / "system.pdb"))
    sources = set(re.findall(r"/opt/ambertools/dat/[^\s\)]+", (output / "prepare.log").read_text()))
    sources.update(str(Path("/opt/ambertools/dat/leap/cmd") / name)
                   for name in ["leaprc.protein.ff14SB", "leaprc.water.tip3p"])
    source_manifest = [{"path": source, "sha256": sha(Path(source))}
                       for source in sorted(sources) if Path(source).is_file()]
    protocol = {"schema": "fs2-md-comparison/v1", "molecule": "ACE-ALA-NME",
                "force_field": "Amber ff14SB", "water_model": "TIP3P",
                "temperature_K": 300.0, "pressure_bar": 1.0,
                "pressure_lammps_real_atm": 1.0 / 1.01325,
                "timestep_fs": 2.0, "cutoff_A": 10.0,
                "lj_switching": False, "lj_potential_shift": False,
                "lj_isotropic_tail_correction": True,
                "long_range": "PME; LAMMPS PPPM", "electrostatic_target_tolerance": 1e-5,
                "single_point_electrostatic_tolerance": 1e-6,
                "constraints": "bonds involving hydrogen; rigid TIP3P water",
                "constraint_tolerance": 1e-6, "thermostat": "native Langevin, all atoms",
                "langevin_friction_per_ps": 1.0,
                "barostat": "native isotropic ensemble-correct method, per-engine differences recorded",
                "minimization_max_iterations": 5000,
                "nvt_steps": 50000, "npt_steps": 50000, "production_steps": 500000,
                "production_ensemble": "NPT", "output_every_steps": 500,
                "nvt_seed": 20260923, "npt_seed": 20260924, "production_seed": 20260925,
                "velocities": "independent native Maxwell initialization at300K after minimization; identical seed does not mean identical RNG",
                "scientific_convergence_claimed": False}
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    manifest = {"status": "prepared-not-converted-or-simulated", "engine_image": ENGINE,
                "parmed_version": parmed.__version__, "commands": [command],
                "atoms": len(top.atoms), "peptide_atoms": len(peptide_indices),
                "water_molecules": len(top.residues) - 3, "total_charge_e": charge,
                "box_A_degrees": list(map(float, top.box)),
                "minimum_peptide_clearance_A_by_axis": clearance.tolist(),
                "requested_solvation_padding_A": 15.0,
                "coordinate_transform": "rigid translation centers peptide bounding box; whole-water translations by integer box vectors",
                "force_field_sources": source_manifest,
                "files": [{"path": path.name, "sha256": sha(path), "bytes": path.stat().st_size}
                          for path in sorted(output.iterdir()) if path.is_file()]}
    (output / "master-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {key: value for key, value in manifest.items() if key not in {"files", "force_field_sources"}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(prepare(parser.parse_args().output), indent=2))
