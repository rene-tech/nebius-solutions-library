"""Prepare reproducible public 1AKI qualification inputs (not a converged study).

Run the resulting native workflow on each GPU without changing these inputs.
All generated outputs belong to the supplied new directory, never source data.
"""

import argparse
import hashlib
import json
import tarfile
import urllib.request
from pathlib import Path

SOURCE = "https://files.rcsb.org/download/1AKI.pdb"
COMMON = """cutoff-scheme = Verlet
coulombtype = PME
rcoulomb = 1.0
rvdw = 1.0
pbc = xyz
nstlist = 20
"""
EM = COMMON + """integrator = steep
nsteps = 5000
emtol = 1000
emstep = 0.01
constraints = none
"""
MD = COMMON + """integrator = md
dt = 0.002
constraints = h-bonds
constraint-algorithm = lincs
tcoupl = v-rescale
tc-grps = System
tau-t = 0.1
ref-t = 300
pcoupl = no
ld-seed = 20260923
nstxout = 0
nstvout = 0
nstfout = 0
nstxout-compressed = 1000
nstenergy = 1000
nstlog = 1000
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=50000)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    data = args.output / "data"
    data.mkdir()
    with urllib.request.urlopen(SOURCE, timeout=45) as response:
        pdb = response.read(2 * 1024**2)
    text = pdb.decode()
    if not text.startswith("HEADER") or "1AKI" not in text[:100]:
        raise ValueError("RCSB response is not the expected 1AKI structure")
    # The qualification protocol retains protein ATOM/TER records, excluding
    # crystal waters and heteroatoms before deliberate solvation.
    protein = "\n".join(line for line in text.splitlines() if line.startswith(("ATOM  ", "TER   "))) + "\nEND\n"
    (data / "protein.pdb").write_text(protein)
    (data / "em.mdp").write_text(EM)
    # This TPR is consumed only by genion, never simulated. Avoid the PME
    # net-charge warning before neutralization without suppressing warnings.
    (data / "ions.mdp").write_text(EM.replace("coulombtype = PME", "coulombtype = Cut-off"))
    (data / "nvt.mdp").write_text(MD + "nsteps = 10000\ngen-vel = yes\ngen-temp = 300\ngen-seed = 20260923\n")
    (data / "md.mdp").write_text(MD + f"nsteps = {args.steps}\ncontinuation = yes\ngen-vel = no\n")
    steps = []

    def step(name, command, argv, stdin=""):
        steps.append({"id": name, "command": command, "args": argv.split(), "stdin": stdin})

    step("topology", "pdb2gmx", "-f protein.pdb -o protein.gro -p topol.top -ff amber99sb-ildn -water tip3p -ignh")
    step("box", "editconf", "-f protein.gro -o box.gro -c -d 1.0 -bt dodecahedron")
    step("solvate", "solvate", "-cp box.gro -cs spc216.gro -o solvated.gro -p topol.top")
    step("ions-tpr", "grompp", "-f ions.mdp -c solvated.gro -p topol.top -o ions.tpr")
    step("neutralize", "genion", "-s ions.tpr -o solvated-ions.gro -p topol.top -pname NA -nname CL -neutral -seed 20260923", "SOL\n")
    step("em-tpr", "grompp", "-f em.mdp -c solvated-ions.gro -p topol.top -o em.tpr")
    step("minimize", "mdrun", "-s em.tpr -deffnm em")
    step("nvt-tpr", "grompp", "-f nvt.mdp -c em.gro -p topol.top -o nvt.tpr")
    step("equilibrate", "mdrun", "-s nvt.tpr -deffnm nvt")
    step("md-tpr", "grompp", "-f md.mdp -c nvt.gro -t fs2-equilibrate.cpt -p topol.top -o md.tpr")
    step("production", "mdrun", "-s md.tpr -deffnm md")
    steps.append({"id": "join-trajectory", "command": "trjcat",
                  "args": ["-f", {"files": "md.part*.xtc"}, "-o", "md.xtc"],
                  "expected_outputs": ["md.xtc"]})
    steps.append({"id": "join-energy", "command": "eneconv",
                  "args": ["-f", {"files": "md.part*.edr"}, "-o", "md.edr"],
                  "expected_outputs": ["md.edr"]})
    step("trajectory-check", "check", "-f md.xtc")
    step("backbone-rmsd", "rms", "-s md.tpr -f md.xtc -o backbone-rmsd.xvg", "Backbone\nBackbone\n")
    step("energies", "energy", "-f md.edr -o energies.xvg", "Potential\nTemperature\n0\n")
    request = {
        "schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
        "jobs": [{"id": "lysozyme", "steps": steps}], "threads": 8,
        "segment_minutes": 0.1, "checkpoint_minutes": 0.1,
        "max_wall_seconds": 3600,
    }
    (args.output / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    with tarfile.open(args.output / "input.tar.gz", "w:gz") as archive:
        for file in sorted(data.iterdir()):
            archive.add(file, arcname=file.name)
    (args.output / "source.json").write_text(json.dumps({
        "source": SOURCE, "source_sha256": hashlib.sha256(pdb).hexdigest(),
        "protein_input_sha256": hashlib.sha256(protein.encode()).hexdigest(),
        "purpose": "Protocol and runtime qualification only; no equilibrium or biological result claimed.",
        "protocol": "AMBER99SB-ILDN/TIP3P, neutralized solvent, minimization, 20 ps NVT, finite NVT production; 2 fs dt.",
        "production_steps": args.steps,
    }, indent=2) + "\n")
    print(str(args.output))


if __name__ == "__main__":
    main()
