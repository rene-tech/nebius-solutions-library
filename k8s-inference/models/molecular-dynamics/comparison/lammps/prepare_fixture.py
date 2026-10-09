#!/usr/bin/env python3
"""Create immutable native probe/production bundles; never execute simulations."""
import argparse
import gzip
import json
from decimal import Decimal
from pathlib import Path
import shutil
import tarfile

from adapter import generate, read_data, sha256

IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/lammps-worker@sha256:e4e21f952285134be263c9ea3f1f06ce461fdca2409622b568b186b12f7f199c"
HEADER = "package kokkos neigh half newton on\nunits real\natom_style full\nboundary p p p\n"
STYLES = "bond_style hybrid harmonic\nangle_style hybrid harmonic\ndihedral_style hybrid charmm multi/harmonic\nspecial_bonds lj 0.0 0.0 0.5 coul 0.0 0.0 0.83333333\n"


def restart_coefficients(sections):
    """Replay exact hybrid substyle coefficients not stored by native restart."""
    lines = []
    for section, command in (("Bond Coeffs", "bond_coeff"), ("Angle Coeffs", "angle_coeff"), ("Dihedral Coeffs", "dihedral_coeff"), ("Improper Coeffs", "improper_coeff")):
        for row in sections.get(section, []):
            lines.append(command + " " + " ".join(row))
    return "\n".join(lines) + "\n"


def make_bundle(directory, archive):
    with archive.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed, tarfile.open(fileobj=compressed, mode="w") as bundle:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                info = bundle.gettarinfo(str(path), str(path.relative_to(directory)))
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                with path.open("rb") as stream:
                    bundle.addfile(info, stream)


def orthogonal_command(header):
    """Geometry-preserving representation only; native command also rejects tilt."""
    records = [line.split() for line in header if line.split()[-3:] == ["xy", "xz", "yz"]]
    if len(records) > 1 or any(len(row) != 6 or any(Decimal(v) != 0 for v in row[:3]) for row in records):
        raise ValueError("orthogonal representation requires exactly zero xy/xz/yz")
    return "change_box all ortho\n"


def prepare(master, converted, output, orthogonal_proof=None):
    master, converted, output = map(Path, (master, converted, output))
    if output.exists():
        raise ValueError("fixture output must be new")
    output.mkdir(parents=True)
    inputs = output / "inputs"
    proof = generate(master, converted, inputs)
    adapted_header, adapted_sections = read_data(inputs / "system-shake.lmp")
    representation = ""
    representation_receipt = None
    if orthogonal_proof:
        qualification = json.loads(Path(orthogonal_proof).read_text())
        if qualification.get("status") != "passed":
            raise ValueError("orthogonal recipe requires passed native paired controls")
        for variant in ("baseline-triclinic", "orthogonal"):
            rows = [row for row in qualification["results"] if row["variant"] == variant]
            if len(rows) != 3 or {row["repetition"] for row in rows} != {1, 2, 3} or any(row["status"] != "passed" or row["profiled_or_synchronous"] for row in rows):
                raise ValueError("three unprofiled matched repetitions required")
        original_header, _ = read_data(converted / "lammps/system.lmp")
        orthogonal_command(original_header)
        representation = orthogonal_command(adapted_header)
        shutil.copy2(orthogonal_proof, inputs / "orthogonal-native-proof.json")
        representation_receipt = {"native_proof_sha256": sha256(inputs / "orthogonal-native-proof.json"), "scope": "same zero-tilt geometry; representation-only command before PPPM and fixes; not final production performance"}
    (inputs / "restart-coefficients.inc").write_text(restart_coefficients(adapted_sections))
    protocol = json.loads((master / "protocol.json").read_text())
    if (protocol["production_steps"], protocol["nvt_steps"], protocol["npt_steps"], protocol["timestep_fs"], protocol["output_every_steps"]) != (500000, 50000, 50000, 2., 500):
        raise ValueError("canonical protocol changed; review generator")
    shutil.copy2(converted / "lammps/system.lmp", inputs / "system-original.lmp")
    shutil.copy2(converted / "lammps/system.input", inputs / "system-original.input")
    shutil.copy2(master / "protocol.json", inputs / "protocol.json")
    shutil.copy2(master / "master-manifest.json", inputs / "master-manifest.json")
    pair_lines = [line for line in (converted / "lammps/system.input").read_text().splitlines() if line.startswith("pair_coeff ")]
    nonbonded = "pair_style lj/cut/coul/long 10.0 10.0\n" + "\n".join(pair_lines) + "\npair_modify mix arithmetic shift no tail yes\nkspace_style pppm 1e-5\nkspace_modify mesh 64 64 64 order 4\nneighbor 2.0 bin\nneigh_modify delay 0 every 1 check yes\ntimestep 2.0\n"
    (inputs / "nonbonded.inc").write_text(nonbonded)
    (inputs / "thermo.inc").write_text("thermo 500\nthermo_style custom step time atoms temp press density vol ebond eangle edihed eimp evdwl ecoul elong etail pe ke etotal\nthermo_modify lost error flush yes format float %.15g\n")
    shake = "fix constraints all shake 1e-6 500 500 b " + " ".join(map(str, proof["shake_bond_types"])) + " a " + " ".join(map(str, proof["shake_angle_types"])) + "\nfix_modify constraints virial yes\n"
    (inputs / "constraints.inc").write_text(shake)
    # Paired native run0 compares original vs adapter at identical coordinates,
    # including force vectors. There are NO SHAKE forces in this static check.
    for name, data in (("original", "system-original.lmp"), ("adapted", "system-shake.lmp")):
        script = HEADER + STYLES + f"read_data {data}\n" + representation + "include nonbonded.inc\nkspace_style pppm 1e-6\nkspace_modify mesh 64 64 64 order 4\ninclude thermo.inc\nrun 0\n"
        script += f"write_dump all custom {name}-forces.lammpstrj id type x y z fx fy fz modify sort id format float %.15g\nprint \"$(pe:%.15g) $(ebond:%.15g) $(eangle:%.15g) $(edihed:%.15g) $(eimp:%.15g) $(evdwl:%.15g) $(ecoul:%.15g) $(elong:%.15g) $(etail:%.15g)\" file {name}-energy.txt screen no\n"
        (inputs / f"static-{name}.in").write_text(script)
    minimize = HEADER + STYLES + "read_data system-shake.lmp\n" + representation + "include nonbonded.inc\ninclude thermo.inc\nmin_style cg\nmin_modify dmax 0.1\nminimize 1e-8 1e-4 5000 50000\nreset_timestep 0\nwrite_restart minimized.restart\nwrite_dump all custom minimized.lammpstrj id type x y z modify sort id format float %.15g\n"
    (inputs / "minimize.in").write_text(minimize)
    pressure = protocol["pressure_lammps_real_atm"]

    def fixes(stage, seed):
        integration = "fix integrate all nve\n" if stage == "nvt" else f"fix integrate all nph iso {pressure:.16g} {pressure:.16g} 2000.0 ptemp 300.0 mtk yes pchain 3\n"
        # Native Kokkos requires SHAKE before a box-changing fix, but SHAKE must
        # also follow Langevin so its correction includes the thermostat forces.
        return f"fix thermal all langevin 300.0 300.0 1000.0 {seed} zero yes\ninclude constraints.inc\n" + integration

    probe = HEADER + "read_restart minimized.restart\n" + representation + "include restart-coefficients.inc\ninclude nonbonded.inc\ninclude thermo.inc\n" + fixes("nvt", 20260923) + "velocity all create 300.0 20260923 mom yes rot no dist gaussian\n"
    probe += "dump coordinates all custom 500 probe.lammpstrj id type x y z vx vy vz\ndump_modify coordinates sort id format float %.15g\nrun 1000\nunfix constraints\nunfix thermal\nunfix integrate\n" + fixes("npt", 20260924)
    probe += "run 1000\nwrite_restart probe.restart\nprint \"$(step:%.0f)\" file probe-progress.txt screen no\n"
    (inputs / "probe.in").write_text(probe)
    stages = []
    for stage, prior, origin, target, seed in (("nvt", "minimized", 0, 50000, 20260923), ("npt", "nvt", 50000, 100000, 20260924), ("production", "npt", 100000, 600000, 20260925)):
        for resume in (False, True):
            read_name = stage if resume else prior
            text = HEADER + f"read_restart {read_name}.restart\n" + representation + "include restart-coefficients.inc\ninclude nonbonded.inc\ninclude thermo.inc\n"
            text += fixes("nvt" if stage == "nvt" else "npt", seed)
            if stage == "nvt" and not resume:
                text += "velocity all create 300.0 20260923 mom yes rot no dist gaussian\n"
            text += f"dump coordinates all custom 500 {stage}.${{fs2_segment}}.lammpstrj id type x y z vx vy vz\ndump_modify coordinates sort id format float %.15g\ntimer timeout ${{fs2_segment_seconds}} every 500\nrun {target} upto\nwrite_restart {stage}.restart\nprint \"$(step:%.0f)\" file {stage}-progress.txt screen no\n"
            (inputs / f"{stage}{'-resume' if resume else ''}.in").write_text(text)
        stages.append({"id": stage, "input": f"{stage}.in", "expected_outputs": [f"{stage}.restart", f"{stage}-progress.txt"], "continuation": {"input": f"{stage}-resume.in", "restart_file": f"{stage}.restart", "progress_file": f"{stage}-progress.txt", "target_step": target}})
    request_base = {"schema": "fs2-serve.nebius.ai/lammps-workflow-request/v1", "backend": "kokkos-cuda", "threads": 4, "segment_seconds": 1800, "max_wall_seconds": 14400, "max_output_bytes": 2147483648, "output_destination": "customer-bucket"}
    probe_request = {**request_base, "max_wall_seconds": 1800, "jobs": [{"id": "adapter-native-proof", "steps": [{"id": f"static-{name}", "input": f"static-{name}.in", "expected_outputs": [f"{name}-energy.txt", f"{name}-forces.lammpstrj"]} for name in ("original", "adapted")] + [{"id": "minimize", "input": "minimize.in", "expected_outputs": ["minimized.restart"]}, {"id": "shake-probe", "input": "probe.in", "expected_outputs": ["probe.restart", "probe-progress.txt", "probe.lammpstrj"]}]}]}
    full_request = {**request_base, "jobs": [{"id": "alanine-canonical", "steps": [{"id": "minimize", "input": "minimize.in", "expected_outputs": ["minimized.restart", "minimized.lammpstrj"]}, *stages]}]}
    (output / "probe-request.json").write_text(json.dumps(probe_request, indent=2) + "\n")
    (output / "production-request.json").write_text(json.dumps(full_request, indent=2) + "\n")
    manifest = {"status": "fixture-prepared-not-native-qualified", "engine_image": IMAGE, "protocol": protocol, "adapter": proof, "production_origin_step": 100000, "production_origin_time_ps": 200., "expected_final_step": 600000, "production_common_frames": 1000, "native_algorithms": {"thermostat": "LAMMPS Langevin, damp1000fs=1ps; native uniform-noise implementation; zero-total-random-force yes", "barostat": "NPH isotropic MTK, Pdamp2000fs=2ps, barostat chain3; external Langevin thermostat", "constraints": "native SHAKE after force-modifying thermostat, tolerance1e-6; peptide H bonds, water OH+HOH", "minimization": "native CG, unconstrained full original harmonic force field; max5000iterations/50000evaluations", "restart": "full native binary state + recreated pair/PPPM/fixes/outputs; Langevin RNG resets on actual restart, not bitwise continuation", "neighbor": "2A skin, every1/checkyes", "long_range": "PPPM1e-5, mesh64^3 order4; static proof1e-6"}, "input_files": [{"path": str(p.relative_to(inputs)), "sha256": sha256(p), "bytes": p.stat().st_size} for p in sorted(inputs.iterdir())]}
    if representation_receipt:
        manifest["box_representation"] = {"mode": "orthogonal", "command": representation.strip(), "unchanged_geometry": "all three source tilts exactly zero; coordinates, lengths, potential and physical protocol retained", **representation_receipt}
    (inputs / "fixture-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    make_bundle(inputs, output / "input.tar.gz")
    manifest["bundle_sha256"] = sha256(output / "input.tar.gz")
    (output / "fixture-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--converted", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--orthogonal-proof", type=Path, help="opt-in zero-tilt representation, requires passed three-pair native qualification JSON")
    args = parser.parse_args()
    manifest = prepare(args.master, args.converted, args.output, args.orthogonal_proof)
    print(json.dumps({"status": manifest["status"], "bundle_sha256": manifest["bundle_sha256"], "water_count": manifest["adapter"]["water_count"]}, indent=2))
