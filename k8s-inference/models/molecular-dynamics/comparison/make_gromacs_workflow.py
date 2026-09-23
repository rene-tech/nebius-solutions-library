"""Build the canonical ff14SB/TIP3P GROMACS workflow without altering topology."""

import argparse
import hashlib
import json
from pathlib import Path

from make_static_fixtures import write_bundle


def make(master, converted, audit, output):
    proof = json.loads(audit.read_text())
    conversion = converted / "conversion.json"
    if proof["status"] != "passed" or proof["input_hashes"][str(conversion)] != hashlib.sha256(conversion.read_bytes()).hexdigest():
        raise ValueError("A passed, matching parameter-equivalence audit is required")
    protocol = json.loads((master / "protocol.json").read_text())
    files = {"system.top": (converted / "gromacs/system.top").read_bytes(),
             "system.gro": (converted / "gromacs/system.gro").read_bytes(),
             "protocol.json": (master / "protocol.json").read_bytes(),
             "master-manifest.json": (master / "master-manifest.json").read_bytes(),
             "topology-audit.json": audit.read_bytes()}
    common = """cutoff-scheme = Verlet
nstlist = 10
verlet-buffer-tolerance = -1
rlist = 1.15
coulombtype = PME
coulomb-modifier = None
rcoulomb = 1.0
ewald-rtol = 1e-5
pme-order = 4
fourier-nx = 64
fourier-ny = 64
fourier-nz = 64
vdwtype = Cut-off
vdw-modifier = None
rvdw = 1.0
DispCorr = EnerPres
pbc = xyz
comm-mode = None
nstenergy = 500
nstcalcenergy = 10
nstlog = 500
"""
    files["minimize.mdp"] = common + """integrator = steep
nsteps = 5000
emtol = 100
emstep = 0.001
define = -DFLEXIBLE
constraints = none
"""
    steps = []
    previous = "system.gro"
    for stage in ("minimize", "nvt", "npt", "production"):
        if stage != "minimize":
            nvt = stage == "nvt"
            seed = protocol[stage + "_seed"]
            count = protocol[stage + "_steps"]
            files[stage + ".mdp"] = common + f"""integrator = sd
dt = 0.002
nsteps = {count}
ld-seed = {seed}
gen-seed = {seed}
tc-grps = System
tau-t = 1.0
ref-t = 300
constraints = h-bonds
constraint-algorithm = lincs
; GROMACS native stable constraint solver; same target H-bond distances.
; LINCS specifies order/iterations, not a SHAKE convergence tolerance.
lincs-order = 8
lincs-iter = 2
continuation = {'no' if nvt else 'yes'}
gen-vel = {'yes' if nvt else 'no'}
gen-temp = 300
nstxout-compressed = 500
compressed-x-precision = 1000000
nstvout = 0
nstfout = 0
pcoupl = {'no' if nvt else 'C-rescale'}
""" + ("""pcoupltype = isotropic
tau-p = 2.0
ref-p = 1.0
compressibility = 4.5e-5
nstpcouple = 10
""" if not nvt else "")
        args = ["-f", stage + ".mdp", "-c", previous, "-p", "system.top", "-o", stage + ".tpr"]
        if stage in {"npt", "production"}:
            prior = "nvt" if stage == "npt" else "npt"
            args += ["-t", "fs2-" + prior + ".cpt"]
        steps.extend([
            {"id": "prepare-" + stage, "command": "grompp", "args": args},
            # Keep the common 64^3 mesh and 1.0 nm Coulomb cutoff fixed for
            # every step, including warm-up. Automatic PP/PME balancing can
            # temporarily benchmark different grids and real-space cutoffs.
            {"id": stage, "command": "mdrun", "args": ["-s", stage + ".tpr", "-deffnm", stage, "-notunepme"],
             "expected_outputs": [stage + ".gro"]},
        ])
        if stage != "minimize":
            steps.append({"id": "energies-" + stage, "command": "energy",
                          "args": ["-f", {"files": stage + "*.edr"}, "-o", stage + "-energy.xvg"],
                          "stdin": "Potential\nTotal-Energy\nTemperature\nPressure\nDensity\n0\n",
                          "expected_outputs": [stage + "-energy.xvg"]})
        previous = stage + ".gro"
    steps.append({"id": "trajectory", "command": "trjcat", "args": ["-f", {"files": "production*.xtc"},
                  "-o", "canonical-production.xtc"], "expected_outputs": ["canonical-production.xtc"]})
    steps.append({"id": "check", "command": "check", "args": ["-f", "canonical-production.xtc"]})
    return write_bundle(output, files, {"schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
                        "threads": 8, "max_wall_seconds": 3600, "max_output_bytes": 2147483648,
                        "segment_minutes": 10, "checkpoint_minutes": 1,
                        "output_destination": "customer-bucket", "jobs": [{"id": "canonical-alanine", "steps": steps}]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("master", "converted", "audit", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(make(args.master, args.converted, args.audit, args.output), indent=2))
