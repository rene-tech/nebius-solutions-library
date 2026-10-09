"""Package identical-coordinate energy/force probes for actual platform Apps."""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile


def write_bundle(path, files, request):
    path.mkdir(parents=True, exist_ok=False)
    data = path / "data"
    data.mkdir()
    for name, value in files.items():
        target = data / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value.encode() if isinstance(value, str) else value)
    (path / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    with (path / "input.tar.gz").open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w") as archive:
            for name in sorted(files):
                value = (data / name).read_bytes()
                item = tarfile.TarInfo(name)
                item.size, item.mode, item.mtime = len(value), 0o644, 0
                archive.addfile(item, io.BytesIO(value))
    manifest = {"files": [{"path": name, "sha256": hashlib.sha256((data / name).read_bytes()).hexdigest()}
                          for name in sorted(files)],
                "input_sha256": hashlib.sha256((path / "input.tar.gz").read_bytes()).hexdigest(),
                "request_sha256": hashlib.sha256((path / "request.json").read_bytes()).hexdigest()}
    (path / "fixture-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def make(master, converted, audit, output):
    checked = json.loads(audit.read_text())
    if checked["status"] != "passed":
        raise ValueError("Parameter equivalence must pass before native energy probes")
    if checked["input_hashes"][str(converted / "conversion.json")] != hashlib.sha256((converted / "conversion.json").read_bytes()).hexdigest():
        raise ValueError("Conversion identity changed after parameter audit")
    output.mkdir(parents=True, exist_ok=False)
    common = {"protocol.json": (master / "protocol.json").read_bytes(),
              "master-manifest.json": (master / "master-manifest.json").read_bytes(),
              "topology-audit.json": audit.read_bytes()}
    gmx_files = {**common, "system.top": (converted / "gromacs/system.top").read_bytes(),
                 "system.gro": (converted / "gromacs/system.gro").read_bytes()}
    lmp_files = {**common, "system.lmp": (converted / "lammps/system.lmp").read_bytes()}
    gmx_jobs, lmp_jobs = [], []
    for tail in (True, False):
        name = "tail-on" if tail else "tail-off"
        gmx_files[name + ".mdp"] = f"""; Exact canonical coordinate energy, no minimization or projection.
integrator = md
nsteps = 0
; No integration occurs. Diagnostic-only dt avoids a flexible-water stability
; warning in grompp; actual production remains the requested2fs withconstraints.
dt = 0.0002
ld-seed = 20260923
gen-seed = 20260923
define = -DFLEXIBLE
constraints = none
continuation = yes
cutoff-scheme = Verlet
nstlist = 10
verlet-buffer-tolerance = -1
rlist = 1.15
coulombtype = PME
coulomb-modifier = None
rcoulomb = 1.0
ewald-rtol = 1e-6
pme-order = 4
fourier-nx = 64
fourier-ny = 64
fourier-nz = 64
vdwtype = Cut-off
vdw-modifier = None
rvdw = 1.0
DispCorr = {'EnerPres' if tail else 'no'}
pbc = xyz
tcoupl = no
pcoupl = no
gen-vel = no
comm-mode = None
nstenergy = 1
nstcalcenergy = 1
nstlog = 1
nstfout = 1
"""
        gmx_jobs.append({"id": "alanine-static-" + name, "steps": [
            {"id": "prepare", "command": "grompp", "args": ["-f", name + ".mdp", "-c", "system.gro", "-p", "system.top", "-o", name + ".tpr"]},
            {"id": "energy", "command": "mdrun", "args": ["-s", name + ".tpr", "-deffnm", name]},
            {"id": "extract", "command": "energy", "args": ["-f", {"files": name + "*.edr"}, "-o", name + "-energy.xvg"],
             "stdin": "Bond\nAngle\nProper-Dih.\nPer.-Imp.-Dih.\nLJ-14\nCoulomb-14\nLJ-(SR)\nCoulomb-(SR)\nCoul.-recip.\nPotential\n" + ("Disper.-corr.\n" if tail else "") + "0\n",
             "expected_outputs": [name + "-energy.xvg"]}]})
        native = (converted / "lammps/system.input").read_text()
        native = native[:native.index("thermo_style")]
        native = native.replace("pair_modify shift no tail yes", "pair_modify shift no tail " + ("yes" if tail else "no"))
        native += "kspace_modify mesh 64 64 64 order 4\nneighbor 2.0 bin\nneigh_modify delay 0 every 1 check yes\n"
        native += "thermo 1\nthermo_style custom step atoms temp ebond eangle edihed eimp evdwl ecoul elong etail pe press vol\nthermo_modify format float %.15g flush yes\nrun 0\n"
        native += f"write_dump all custom {name}-forces.lammpstrj id type x y z fx fy fz modify sort id format float %.15g\n"
        native += f"print \"$(pe:%.15g)\" file {name}-energy.txt screen no\n"
        lmp_files[name + ".in"] = "package kokkos neigh half newton on\n" + native
        lmp_jobs.append({"id": "alanine-static-" + name, "steps": [{"id": "energy", "input": name + ".in",
                         "expected_outputs": [name + "-forces.lammpstrj", name + "-energy.txt"]}]})
    results = {}
    results["gromacs"] = write_bundle(output / "gromacs", gmx_files,
        {"schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1", "threads": 4,
         "max_wall_seconds": 1800, "max_output_bytes": 1073741824, "jobs": gmx_jobs})
    results["lammps"] = write_bundle(output / "lammps", lmp_files,
        {"schema": "fs2-serve.nebius.ai/lammps-workflow-request/v1", "backend": "kokkos-cuda",
         "threads": 4, "max_wall_seconds": 1800, "max_output_bytes": 1073741824,
         "output_destination": "customer-bucket", "jobs": lmp_jobs})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("master", "converted", "audit", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(make(args.master, args.converted, args.audit, args.output), indent=2))
