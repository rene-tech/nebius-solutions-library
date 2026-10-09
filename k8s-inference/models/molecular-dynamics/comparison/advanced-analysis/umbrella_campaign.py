#!/usr/bin/env python3
"""Immutable native GROMACS phi-umbrella inputs and exact-binary CV audit.

This prepares new simulations; it never changes the frozen four-engine delivery.
Native submission uses the existing typed customer SDK, not direct GPU Pods.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime, timezone

MD = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MD / "qualification/dense-motion-20260924"))
from gromacs_amber import archive, mdp_values, record, require, save

IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs@sha256:14ffdae0f0389c7771bae8791c56a5e21736ece630bd11dfb0f3b6a78f8cd643"
CLIENT = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d"
GMX = "/usr/local/gromacs/avx2_256/bin/gmx"
PHI = [4, 6, 8, 14]
PSI = [6, 8, 14, 16]
KAPPA = 200.0  # kJ mol^-1 rad^-2, NOT per degree squared.


def circular_delta(a, b):
    return (a - b + 180) % 360 - 180


def rotate_phi(positions, bonds, target):
    """Rotate one bonded component, preserving every bond and stereocenter."""
    import numpy as np
    sys.path.insert(0, str(MD / "comparison/analysis"))
    from geometry import dihedral
    graph = [set() for _ in positions]
    for a, b in bonds:
        if {int(a), int(b)} != {PHI[1], PHI[2]}:
            graph[a].add(int(b)); graph[b].add(int(a))
    moving, queue = {PHI[2]}, [PHI[2]]
    while queue:
        for atom in graph[queue.pop()] - moving:
            moving.add(atom); queue.append(atom)
    require(PHI[1] not in moving and PHI[3] in moving, "rotation bond must split the peptide")
    moving = sorted(moving)
    axis = positions[PHI[2]] - positions[PHI[1]]
    axis /= np.linalg.norm(axis)
    angle = np.deg2rad(circular_delta(target, dihedral(positions[PHI])))
    center = positions[PHI[1]]
    v = positions[moving] - center
    rotated = positions.copy()
    rotated[moving] = center + v * np.cos(angle) + np.cross(axis, v) * np.sin(angle) + np.outer(v @ axis, axis) * (1 - np.cos(angle))
    error = abs(circular_delta(dihedral(rotated[PHI]), target))
    require(error < 1e-7, "constructed phi has wrong sign or angle")
    distances = lambda x: np.linalg.norm(x[bonds[:, 0]] - x[bonds[:, 1]], axis=1)
    bond_error = float(np.max(np.abs(distances(rotated) - distances(positions))))
    require(bond_error < 1e-10, "dihedral construction changed bond lengths")
    psi_error = abs(circular_delta(dihedral(rotated[PSI]), dihedral(positions[PSI])))
    require(psi_error < 1e-7, "dihedral construction changed psi")
    def chirality(x):
        n, ca, c, cb = x[[6, 8, 14, 10]]
        return float(np.dot(np.cross(n - ca, c - ca), cb - ca))
    require(chirality(rotated) * chirality(positions) > 0, "alanine chirality changed")
    return rotated, {"rotated_atom_indices0": moving, "phi_degrees": dihedral(rotated[PHI]),
                     "psi_degrees": dihedral(rotated[PSI]), "bond_length_error_A": bond_error,
                     "psi_error_degrees": psi_error, "chirality_preserved": True}


def pull_parameters(center):
    result = {"pull": "yes", "pull-ngroups": "5", "pull-ncoords": "2",
              "pull-nstxout": "50", "pull-nstfout": "50", "pull-print-ref-value": "no",
              "pull-print-components": "no", "pull-print-com": "no",
              "pull-xout-average": "no", "pull-fout-average": "no"}
    for number, name in enumerate(("ACE_C", "ALA_N", "ALA_CA", "ALA_C", "NME_N"), 1):
        result[f"pull-group{number}-name"] = name
    for number, groups, ref, force in ((1, "1 2 2 3 3 4", center, KAPPA), (2, "2 3 3 4 4 5", 0, 0)):
        prefix = f"pull-coord{number}"
        result.update({f"{prefix}-type": "umbrella", f"{prefix}-geometry": "dihedral",
                       f"{prefix}-groups": groups, f"{prefix}-dim": "Y Y Y",
                       f"{prefix}-start": "no", f"{prefix}-init": str(ref),
                       f"{prefix}-rate": "0", f"{prefix}-k": str(force)})
    return result


def write_mdp(path, values):
    path.write_text("; Native phi umbrella; radians force constant, degrees reference.\n" +
                    "".join(f"{key} = {value}\n" for key, value in values.items()))


def workflow(window):
    steps = []
    previous = "start.gro"
    for stage in ("minimize", "nvt", "npt", "production"):
        args = ["-f", stage + ".mdp", "-c", previous, "-p", "system.top", "-n", "dihedrals.ndx", "-o", stage + ".tpr"]
        if stage in ("npt", "production"):
            args += ["-t", "fs2-" + ("nvt" if stage == "npt" else "npt") + ".cpt"]
        steps.append({"id": "prepare-" + stage, "command": "grompp", "args": args, "expected_outputs": [stage + ".tpr"]})
        steps.append({"id": stage, "command": "mdrun", "args": ["-s", stage + ".tpr", "-deffnm", stage, "-notunepme"], "expected_outputs": [stage + ".gro"]})
        if stage != "minimize":
            energy_terms = ["Potential", "Temperature", "Pressure"]
            if stage != "nvt":
                energy_terms.append("Volume")
            steps.append({"id": "energies-" + stage, "command": "energy", "args": ["-f", {"files": stage + "*.edr"}, "-o", stage + "-energy.xvg"],
                          "stdin": "\n".join(energy_terms + ["0", ""]), "expected_outputs": [stage + "-energy.xvg"]})
        previous = stage + ".gro"
    steps.append({"id": "trajectory", "command": "trjcat", "args": ["-f", {"files": "production.part*.xtc"}, "-o", "production-canonical.xtc"], "expected_outputs": ["production-canonical.xtc"]})
    return {"schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1", "threads": 8,
            "checkpoint_minutes": 1, "segment_minutes": 10, "max_wall_seconds": 3600,
            "max_output_bytes": 1073741824, "output_destination": "platform-artifacts",
            "output_prefix": "runs/alanine-umbrella-20260924", "jobs": [{"id": window, "steps": steps}]}


def prepare(delivery, output):
    import numpy as np
    import parmed
    import MDAnalysis as mda
    from MDAnalysis.lib.mdamath import triclinic_vectors
    sys.path.insert(0, str(MD / "comparison/analysis"))
    from compare import master_data
    from geometry import make_whole, dihedral
    require(not output.exists() and not output.is_relative_to(delivery), "use new output outside frozen delivery")
    master = master_data(delivery / "master")
    require(list(master["phi"]) == PHI and list(master["psi"]) == PSI, "canonical torsion indices differ")
    data = delivery / "runs/gromacs/data"
    source_inventory = {r["path"]: r for r in json.loads((delivery / "runs/gromacs/result.json").read_text())["files"]}
    source_files = ["system.top", "production.gro", "minimize.mdp", "nvt.mdp", "npt.mdp", "production.mdp"]
    for name in source_files:
        require(record(data / name)["sha256"] == source_inventory[name]["sha256"], "frozen GROMACS source changed")
    source = mda.Universe(str(delivery / "master/system.prmtop"), str(data / "production.gro"))
    positions = source.atoms.positions.astype(float).copy()
    positions[:22] = make_whole(positions[:22], master["bonds"], triclinic_vectors(source.dimensions, dtype=float))
    topology = parmed.load_file(str(delivery / "master/system.prmtop"))
    topology.box = source.dimensions.copy()
    output.mkdir(parents=True)
    inventory = {"temperature_k": 300, "evidence_kind": "planned-native-md", "worker_image": IMAGE,
                 "client_image": CLIENT, "kappa_kj_mol_rad2": KAPPA,
                 "source_files": [record(data / n) for n in source_files],
                 "preparation": "Rotate only the N-CA downstream bonded component of the equilibrated final GROMACS peptide; solvent unchanged. Native restrained minimization then100psNVT+100psNPT, then2000psproduction.",
                 "windows": [], "source_helper": record(__file__)}
    for index, center in enumerate(range(-180, 180, 15)):
        name = f"window-{index:02d}"
        fixture = output / name
        inputs = fixture / "inputs"
        inputs.mkdir(parents=True)
        xyz, construction = rotate_phi(positions[:22], master["bonds"], center)
        coords = positions.copy(); coords[:22] = xyz
        topology.coordinates = coords
        parmed.gromacs.GromacsGroFile.write(topology, str(inputs / "start.gro"), precision=7, combine="all")
        loaded = mda.Universe(str(delivery / "master/system.prmtop"), str(inputs / "start.gro"))
        construction["serialized_phi_degrees"] = dihedral(loaded.atoms.positions[PHI])
        require(abs(circular_delta(construction["serialized_phi_degrees"], center)) < .001, "serialized phi differs")
        shutil.copyfile(data / "system.top", inputs / "system.top")
        # A custom index replaces the default groups, so include System for
        # the thermostat/COM groups used by the unchanged reference protocol.
        (inputs / "dihedrals.ndx").write_text(
            "[ System ]\n" + "\n".join(" ".join(str(i + 1) for i in range(start, min(start + 15, len(coords))))
                                      for start in range(0, len(coords), 15)) + "\n" +
            "".join(f"[ {group} ]\n{atom + 1}\n" for group, atom in
                    zip(("ACE_C", "ALA_N", "ALA_CA", "ALA_C", "NME_N"), (4, 6, 8, 14, 16))))
        stages = {}
        for stage in ("minimize", "nvt", "npt", "production"):
            settings = mdp_values((data / (stage + ".mdp")).read_text())
            changes = {}
            if stage != "minimize":
                changes = {"ld-seed": str(202609240 + index * 3 + ("nvt", "npt", "production").index(stage)),
                           "gen-seed": str(202609240 + index * 3), "nstenergy": "50", "nstlog": "500", "nstxout-compressed": "500"}
            if stage == "production":
                changes["nsteps"] = "1000000"
            settings.update(changes); settings.update(pull_parameters(center))
            write_mdp(inputs / (stage + ".mdp"), settings)
            stages[stage] = {"changes_from_frozen_stage": changes, "parameters": settings}
        provenance = {"window_id": name, "center_degrees": center, "force_constant_kj_mol_rad2": KAPPA,
                      "construction": construction, "stages": stages, "psi_bias_kappa": 0,
                      "dt_ps": .002, "production_steps": 1000000, "production_duration_ps": 2000,
                      "phi_psi_cadence_ps": .1, "trajectory_cadence_ps": 1, "source": inventory["source_files"]}
        save(inputs / "protocol.json", provenance)
        archive(inputs, fixture / "input.tar.gz")
        save(fixture / "request.json", workflow(name))
        save(fixture / "preparation.json", {**provenance, "files": [record(p) for p in sorted(inputs.iterdir())], "bundle": record(fixture / "input.tar.gz"), "request": record(fixture / "request.json")})
        inventory["windows"].append({"id": name, "center_degrees": center, "fixture": str(fixture), "input": record(fixture / "input.tar.gz")})
    save(output / "preparation.json", inventory)
    print(json.dumps({"status": "prepared-not-simulated", "windows": len(inventory["windows"]), "output": str(output)}))


def native_cv_audit(fixture, output):
    """Exact binary single-frame CPU rerun: sign, units and energy, not GPU proof."""
    import numpy as np
    sys.path.insert(0, str(MD / "comparison/analysis"))
    from geometry import dihedral
    import MDAnalysis as mda
    require(not output.exists(), "use a new audit directory")
    output.mkdir(parents=True)
    for name in ("system.top", "start.gro", "dihedrals.ndx"):
        shutil.copyfile(fixture / "inputs" / name, output / name)
    settings = mdp_values((fixture / "inputs/production.mdp").read_text())
    settings.update({"nsteps": "0", "pcoupl": "no", "gen-vel": "no", "continuation": "yes", "pull-nstxout": "1", "pull-nstfout": "1", "nstenergy": "1"})
    # Offset the bias by10degrees: a nonzero analytic energy verifies radians.
    settings["pull-coord1-init"] = str(circular_delta(float(settings["pull-coord1-init"]) + 10, 0))
    write_mdp(output / "audit.mdp", settings)
    commands = [
        (["--version"], ""),
        (["grompp", "-f", "audit.mdp", "-c", "start.gro", "-p", "system.top", "-n", "dihedrals.ndx", "-o", "audit.tpr"], ""),
        (["mdrun", "-s", "audit.tpr", "-rerun", "start.gro", "-deffnm", "audit", "-ntmpi", "1", "-ntomp", "2", "-nb", "cpu", "-pme", "cpu", "-bonded", "cpu", "-update", "cpu"], ""),
        (["energy", "-f", "audit.edr", "-o", "pull-energy.xvg"], "COM-Pull-En.\n0\n")]
    receipt = {"status": "incomplete", "scope": "CPU exact-binary CV/energy audit only", "image": IMAGE, "commands": [], "input_fixture": record(fixture / "preparation.json")}
    try:
        for index, (arguments, stdin) in enumerate(commands):
            command = ["docker", "run", "--rm", "-i", "--network", "none", "--user", "1002:1002", "--env", "OMP_NUM_THREADS=2", "--entrypoint", GMX, "--mount", f"type=bind,src={output},dst=/audit", "--workdir", "/audit", IMAGE, *arguments]
            run = subprocess.run(command, input=stdin, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            log = output / f"command-{index:02d}.log"; log.write_text(run.stdout)
            receipt["commands"].append({"argv": arguments, "stdin": stdin, "exit_code": run.returncode, "log": record(log)})
            require(run.returncode == 0, "exact-binary CV audit failed; see saved native log")
        def xvg(path):
            return np.atleast_2d(np.loadtxt([line for line in path.read_text().splitlines() if line and not line.startswith(("#", "@"))]))
        pull = xvg(output / "audit_pullx.xvg")
        require(pull.shape == (1, 3), "expected time,phi,psi")
        source = mda.Universe(str(output / "start.gro"))
        phi, psi = dihedral(source.atoms.positions[PHI]), dihedral(source.atoms.positions[PSI])
        # Expected column meanings are confirmed against exact-binary headers.
        require(abs(circular_delta(pull[0, 1], phi)) < .001 and abs(circular_delta(pull[0, 2], psi)) < .001, "native CV sign/order differs from canonical phi/psi")
        expected = .5 * KAPPA * np.deg2rad(circular_delta(phi, float(settings["pull-coord1-init"]))) ** 2
        energy = float(xvg(output / "pull-energy.xvg")[0, 1])
        require(abs(energy - expected) < .005, "native bias energy does not match radian harmonic convention")
        receipt.update(status="passed", phi_degrees=phi, psi_degrees=psi, pullx_row=pull[0].tolist(), expected_bias_kj_mol=expected, native_bias_kj_mol=energy)
    except Exception as error:
        receipt.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        receipt["files"] = [record(p) for p in sorted(output.iterdir()) if p.is_file()]
        save(output / "receipt.json", receipt)
    print(json.dumps({k:receipt[k] for k in ("status", "phi_degrees", "psi_degrees", "expected_bias_kj_mol", "native_bias_kj_mol")}))


def discover(key_file, output):
    import httpx
    secret = json.loads(key_file.read_text())["secret"]
    with httpx.Client(base_url="https://89.169.99.188", headers={"authorization":"Bearer " + secret}, timeout=30, trust_env=False) as client:
        replies = [client.get(path) for path in ("/v1/me", "/v1/scientific-models")]
        require(all(r.status_code == 200 for r in replies), "normal-key discovery failed")
    row = next(r for r in replies[1].json()["data"] if r["model_id"] == "gromacs")
    require(row["runtime_image_digest"].endswith(IMAGE.split("@")[1]), "deployed GROMACS digest changed")
    value = {"status": "passed", "at": datetime.now(timezone.utc).isoformat(), "identity": replies[0].json(), "gromacs": row, "client_image": CLIENT, "no_submission": True}
    save(output, value)
    print(json.dumps({"status":"passed", "gromacs_image": row["runtime_image_digest"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare"); p.add_argument("--delivery", type=Path, required=True); p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("audit-cv"); p.add_argument("--fixture", type=Path, required=True); p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("discover"); p.add_argument("--key-file", type=Path, required=True); p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare": prepare(args.delivery.resolve(), args.output.resolve())
    elif args.command == "audit-cv": native_cv_audit(args.fixture.resolve(), args.output.resolve())
    else: discover(args.key_file, args.output)


if __name__ == "__main__":
    main()
