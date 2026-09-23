"""Generate reproducible native bundles from assets in the pinned NVIDIA image.

Each fixture is a scientific protocol plus explicit native restart context, not a
JSON reconstruction of an arbitrary LAMMPS script. No convergence claim follows.
"""

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path

SOURCE_IMAGE = "nvcr.io/nvidia/lammps@sha256:d8a0076dfe84fcbc98db05531993c1cd9deb964050b9c122b92655dc3685d731"


def fixture(name, assets, steps, *, warmup=2000, segment_seconds=60, threads=1):
    files = {}

    def asset(relative):
        path = assets / relative
        files[path.name] = path.read_bytes()
        return path.name

    if name == "lj":
        setup = "units lj\natom_style atomic\nlattice fcc 0.8442\nregion box block 0 32 0 32 0 32\ncreate_box 1 box\ncreate_atoms 1 box\nmass 1 1.0\nvelocity all create 1.44 87287 loop geom\n"
        physics = "pair_style lj/cut 2.5\npair_coeff 1 1 1.0 1.0 2.5\nneighbor 0.3 bin\nneigh_modify delay 0 every 20 check no\nfix integrator all nve\ntimestep 0.005\n"
        unit, dt, atoms = "lj", 0.005, 131072
    elif name == "eam":
        asset("bench/Cu_u3.eam")
        setup = "units metal\natom_style atomic\nlattice fcc 3.615\nregion box block 0 20 0 20 0 20\ncreate_box 1 box\ncreate_atoms 1 box\nvelocity all create 1600.0 376847 loop geom\n"
        physics = "pair_style eam\npair_coeff 1 1 Cu_u3.eam\nneighbor 1.0 bin\nneigh_modify every 1 delay 5 check yes\nfix integrator all nve\ntimestep 0.005\n"
        unit, dt, atoms = "metal", 0.005, 32000
    elif name == "tersoff":
        asset("potentials/Si.tersoff")
        setup = "units metal\natom_style atomic\nlattice diamond 5.431\nregion box block 0 20 0 20 0 10\ncreate_box 1 box\ncreate_atoms 1 box\nmass 1 28.06\nvelocity all create 1000.0 376847 loop geom\n"
        physics = "pair_style tersoff\npair_coeff * * Si.tersoff Si\nneighbor 1.0 bin\nneigh_modify delay 5 every 1\nfix integrator all nve\ntimestep 0.001\n"
        unit, dt, atoms = "metal", 0.001, 32000
    elif name == "snap":
        for path in ("Ta06A.snap", "Ta06A.snapcoeff", "Ta06A.snapparam"):
            asset("potentials/" + path)
        setup = "units metal\natom_style atomic\nlattice bcc 3.316\nregion box block 0 10 0 10 0 10\ncreate_box 1 box\ncreate_atoms 1 box\nmass 1 180.94788\nvelocity all create 300.0 4928459 loop geom\n"
        physics = "include Ta06A.snap\nneighbor 1.0 bin\nneigh_modify every 1 delay 0 check yes\nfix integrator all nve\ntimestep 0.001\n"
        unit, dt, atoms = "metal", 0.001, 2000
    elif name == "reaxff":
        asset("potentials/ffield.reax.cho")
        setup = "units real\natom_style charge\nlattice diamond 3.567\nregion box block 0 10 0 10 0 10\ncreate_box 1 box\ncreate_atoms 1 box\nmass 1 12.011\nset type 1 charge 0.0\nvelocity all create 300.0 918273 loop geom\n"
        physics = "pair_style reaxff NULL\npair_coeff * * ffield.reax.cho C\nneighbor 2.0 bin\nneigh_modify every 1 delay 0 check yes\nfix charges all qeq/reaxff 1 0.0 10.0 1.0e-6 reaxff\nfix integrator all nve\ntimestep 0.1\n"
        unit, dt, atoms = "real", 0.1, 8000
    elif name == "rhodo":
        asset("bench/data.rhodo")
        setup = "units real\natom_style full\nbond_style harmonic\nangle_style charmm\ndihedral_style charmm\nimproper_style harmonic\npair_style lj/charmm/coul/long 8.0 10.0\nread_data data.rhodo\n"
        physics = "neigh_modify delay 5 every 1\npair_modify mix arithmetic\nkspace_style pppm 1e-4\nfix constraints all shake 0.0001 5 0 m 1.0 a 232\nfix integrator all npt temp 300.0 300.0 100.0 z 0.0 0.0 1000.0 mtk no pchain 0 tchain 1\nspecial_bonds charmm\ntimestep 2.0\n"
        unit, dt, atoms = "real", 2.0, 32000
    else:
        raise ValueError("unknown fixture")
    thermo = "thermo 500\nthermo_style custom step atoms temp pe ke etotal press vol\nthermo_modify lost error flush yes format float %.15g\n"
    files["protocol.inc"] = (physics + thermo).encode()
    files["in.prepare"] = (setup + "include protocol.inc\nrun " + str(warmup) + "\nwrite_restart prepared.restart\n").encode()
    target = warmup + steps
    continuation = (
        "include protocol.inc\n"
        "dump trajectory all custom 1000 trajectory.${fs2_segment}.lammpstrj id type x y z vx vy vz\n"
        "dump_modify trajectory sort id format float %.15g\n"
        "timer timeout ${fs2_segment_seconds} every 100\n"
        f"run {target} upto\n"
        "write_restart state.restart\n"
        "print \"$(step:%.0f)\" file progress.txt screen no\n"
    )
    files["in.production"] = ("read_restart prepared.restart\n" + continuation).encode()
    files["in.resume"] = ("read_restart state.restart\n" + continuation).encode()
    files["in.analyze"] = ("read_restart state.restart\ninclude protocol.inc\nrun 0\nwrite_dump all custom final.lammpstrj id type x y z vx vy vz modify sort id format float %.15g\nprint \"$(step:%.0f) $(atoms:%.0f) $(temp:%.15g) $(pe:%.15g) $(ke:%.15g) $(etotal:%.15g) $(press:%.15g) $(vol:%.15g)\" file final-thermo.txt screen no\n").encode()
    protocol = {"fixture": name, "source_image": SOURCE_IMAGE, "units": unit, "timestep": dt, "warmup_steps": warmup, "production_steps": steps, "target_step": target, "expected_atoms": atoms, "ensemble": "NPT" if name == "rhodo" else "NVE", "seed_policy": "fixed native seed; benchmark repetitions are repeated experiments, not claimed independent scientific replicas", "trajectory_every_steps": 1000, "reduced_time_conversion": None if unit == "lj" else "metal ps; real fs", "scientific_convergence_claimed": False}
    files["protocol.json"] = (json.dumps(protocol, indent=2) + "\n").encode()
    body = {"schema": "fs2-serve.nebius.ai/lammps-workflow-request/v1", "backend": "kokkos-cuda", "threads": threads, "segment_seconds": segment_seconds, "max_wall_seconds": 7200, "max_output_bytes": 4294967296, "output_destination": "platform-artifacts", "jobs": [{"id": name, "steps": [{"id": "prepare", "input": "in.prepare", "expected_outputs": ["prepared.restart"]}, {"id": "production", "input": "in.production", "expected_outputs": ["state.restart", "progress.txt"], "continuation": {"input": "in.resume", "restart_file": "state.restart", "progress_file": "progress.txt", "target_step": target}}, {"id": "analyze", "input": "in.analyze", "expected_outputs": ["final.lammpstrj", "final-thermo.txt"]}]}]}
    return body, files


def write_fixture(destination, body, files):
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "request.json").write_text(json.dumps(body, indent=2) + "\n")
    with tarfile.open(destination / "input.tar.gz", "w:gz") as archive:
        for name, content in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mtime, member.mode = len(content), 0, 0o644
            archive.addfile(member, io.BytesIO(content))
    (destination / "fixture-manifest.json").write_text(json.dumps({name: {"size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()} for name, content in files.items()}, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=("lj", "eam", "tersoff", "snap", "reaxff", "rhodo"), required=True)
    parser.add_argument("--steps", type=int, default=100000)
    parser.add_argument("--warmup", type=int, default=2000)
    parser.add_argument("--segment-seconds", type=int, default=60)
    args = parser.parse_args()
    body, files = fixture(args.case, args.assets, args.steps, warmup=args.warmup, segment_seconds=args.segment_seconds)
    write_fixture(args.output, body, files)


if __name__ == "__main__":
    main()
