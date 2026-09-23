"""Bind the shared ff14SB/TIP3P master to explicit native AMBER NAMD inputs.

No topology or coordinate conversion is performed. Single-point decomposition
must be checked before using the separate, complete dynamics request.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tarfile

from fs2_gromacs.files import digest_file
from fs2_namd import PARAMETER_SCHEMA
from fs2_namd.contracts import canonical, normalize


def parm7(path):
    sections = {}
    for block in re.split(r"(?m)^%FLAG ", path.read_text())[1:]:
        name, *lines = block.splitlines()
        if name in sections:
            raise ValueError("duplicate AMBER topology section")
        fmt = next((re.fullmatch(r"%FORMAT\((\d+)([aAiIeEfFdD])(\d+)(?:\.\d+)?\)", line) for line in lines if line.startswith("%FORMAT")), None)
        if not fmt:
            raise ValueError("unsupported AMBER topology format")
        kind, width = fmt[2].upper(), int(fmt[3])
        values = []
        for line in lines:
            if line.startswith("%"):
                continue
            for start in range(0, len(line), width):
                value = line[start:start + width].strip()
                if value:
                    values.append(value if kind == "A" else int(value) if kind == "I" else float(value.replace("D", "E")))
        sections[name] = values
    if not sections:
        raise ValueError("empty AMBER topology")
    return sections


def audit_master(master):
    manifest = json.loads((master / "master-manifest.json").read_text())
    files = {row["path"]: row for row in manifest["files"]}
    selected = ("system.prmtop", "system.rst7", "system.pdb", "protocol.json")
    for name in selected:
        if digest_file(master / name) != files[name]["sha256"] or (master / name).stat().st_size != files[name]["bytes"]:
            raise ValueError("canonical master identity changed: " + name)
    sections = parm7(master / "system.prmtop")
    atoms = sections["POINTERS"][0]
    if atoms != manifest["atoms"] or any(len(sections[key]) != atoms for key in ("ATOM_NAME", "MASS", "CHARGE", "ATOM_TYPE_INDEX", "NUMBER_EXCLUDED_ATOMS")):
        raise ValueError("canonical atom counts disagree")
    if not all(math.isfinite(v) for key in ("MASS", "CHARGE", "LENNARD_JONES_ACOEF", "LENNARD_JONES_BCOEF") for v in sections[key]):
        raise ValueError("non-finite canonical force-field data")
    if any(v <= 0 for v in sections["MASS"]) or any(v < 0 for v in sections["NONBONDED_PARM_INDEX"]):
        raise ValueError("this native AMBER comparison does not qualify massless sites or 10-12 potentials")
    if any(v != 0 for key in ("HBOND_ACOEF", "HBOND_BCOEF") for v in sections[key]):
        raise ValueError("unsupported AMBER 10-12 terms")
    excluded, offset = set(), 0
    for atom, count in enumerate(sections["NUMBER_EXCLUDED_ATOMS"], start=1):
        entries = sections["EXCLUDED_ATOMS_LIST"][offset:offset + count]
        if len(entries) != count or any(v < 0 or v > atoms for v in entries):
            raise ValueError("invalid canonical exclusions")
        excluded.update(tuple(sorted((atom, other))) for other in entries if other)
        offset += count
    if offset != len(sections["EXCLUDED_ATOMS_LIST"]):
        raise ValueError("canonical exclusion counts disagree")
    pairs = {}
    torsions, ignored = 0, 0
    for name in ("DIHEDRALS_INC_HYDROGEN", "DIHEDRALS_WITHOUT_HYDROGEN"):
        values = sections[name]
        if len(values) % 5:
            raise ValueError("invalid canonical torsion record")
        for index in range(0, len(values), 5):
            a, b, c, d, parameter = values[index:index + 5]
            torsions += 1
            if c < 0 or d < 0:  # suppressed 1-4 or improper: no active 1-4 term
                ignored += 1
                continue
            pair = tuple(sorted((abs(a) // 3 + 1, abs(d) // 3 + 1)))
            scaling = (sections["SCEE_SCALE_FACTOR"][parameter - 1], sections["SCNB_SCALE_FACTOR"][parameter - 1])
            if pair not in excluded or any(not math.isfinite(v) or v <= 0 for v in scaling):
                raise ValueError("active canonical 1-4 pair lacks valid exclusion/scaling")
            if pair in pairs and pairs[pair] != scaling:
                raise ValueError("conflicting canonical 1-4 scaling")
            pairs[pair] = scaling
    factors = sorted(set(pairs.values()))
    if factors != [(1.2, 2.0)]:
        raise ValueError("uniform native ff14SB 1-4 mapping is not valid for this master")
    fields = ("MASS", "CHARGE", "ATOM_TYPE_INDEX", "BOND_FORCE_CONSTANT", "BOND_EQUIL_VALUE",
              "ANGLE_FORCE_CONSTANT", "ANGLE_EQUIL_VALUE", "DIHEDRAL_FORCE_CONSTANT", "DIHEDRAL_PERIODICITY",
              "DIHEDRAL_PHASE", "LENNARD_JONES_ACOEF", "LENNARD_JONES_BCOEF", "NONBONDED_PARM_INDEX",
              "SCEE_SCALE_FACTOR", "SCNB_SCALE_FACTOR", "NUMBER_EXCLUDED_ATOMS", "EXCLUDED_ATOMS_LIST",
              "BONDS_INC_HYDROGEN", "BONDS_WITHOUT_HYDROGEN", "ANGLES_INC_HYDROGEN",
              "ANGLES_WITHOUT_HYDROGEN", "DIHEDRALS_INC_HYDROGEN", "DIHEDRALS_WITHOUT_HYDROGEN")
    return {"status": "canonical-input-audited-not-native-equivalence-proven", "atoms": atoms,
            "total_charge_e": sum(sections["CHARGE"]) / 18.2223, "native_topology_conversion": False,
            "master_manifest_sha256": digest_file(master / "master-manifest.json"),
            "canonical_files": {name: files[name] for name in selected},
            "parameter_field_sha256": {key: hashlib.sha256(canonical(sections[key])).hexdigest() for key in fields},
            "explicit_exclusion_pairs": len(excluded), "torsion_records": torsions,
            "bond_records": sum(len(sections[key]) // 3 for key in ("BONDS_INC_HYDROGEN", "BONDS_WITHOUT_HYDROGEN")),
            "angle_records": sum(len(sections[key]) // 4 for key in ("ANGLES_INC_HYDROGEN", "ANGLES_WITHOUT_HYDROGEN")),
            "torsions_not_generating_1_4": ignored, "active_1_4_pairs": len(pairs),
            "active_1_4_scaling": {"SCEE": 1.2, "SCNB": 2.0, "native_oneFourScaling": 1 / 1.2, "native_scnb": 2.0},
            "box_A_degrees": manifest["box_A_degrees"],
            "limitations": ["Native parser/log and decomposed single-point energies still require validation",
                            "Inactive improper/suppressed torsions may contain zero scaling entries; those are not active 1-4 pairs",
                            "No arbitrary AMBER force-field, virtual-site, CMAP or nonuniform 1-4 mapping claim"]}


def common(protocol, *, singlepoint=False, tail=True):
    tolerance = protocol["single_point_electrostatic_tolerance"] if singlepoint else protocol["electrostatic_target_tolerance"]
    return f"""amber on
oldParmReader off
parmfile system.prmtop
ambercoor system.rst7
readexclusions on
exclude scaled1-4
oneFourScaling {1 / 1.2:.17g}
scnb 2.0
GPUresident on
GPUAtomMigration off
GPUForceTable on
timestep {protocol['timestep_fs']}
nonbondedFrequency 1
fullElectFrequency 1
stepsPerCycle 20
cutoff {protocol['cutoff_A']}
switching off
LJcorrection {'on' if tail else 'off'}
pairlistdist {protocol['cutoff_A'] + 2.0}
PME on
PMETolerance {tolerance}
PMEGridSpacing 1.0
PMEInterpOrder 4
rigidBonds {'none' if singlepoint else 'all'}
rigidTolerance {protocol['constraint_tolerance']}
rigidIterations 100
COMmotion no
useGroupPressure yes
useFlexibleCell no
useConstantArea no
wrapAll off
wrapWater off
outputEnergies {protocol['output_every_steps']}
outputPressure {protocol['output_every_steps']}
outputTiming {protocol['output_every_steps']}
outputEnergiesPrecision 8
DCDfreq {protocol['output_every_steps']}
XSTfreq {protocol['output_every_steps']}
restartfreq 5000
"""


def cell(box):
    a, b, c, alpha, beta, gamma = box
    if (alpha, beta, gamma) != (90.0, 90.0, 90.0):
        raise ValueError("canonical fixture currently requires an orthorhombic cell")
    return f"cellBasisVector1 {a} 0 0\ncellBasisVector2 0 {b} 0\ncellBasisVector3 0 0 {c}\ncellOrigin {a/2} {b/2} {c/2}\n"


def thermostat(protocol, *, pressure=False):
    result = f"langevin on\nlangevinTemp {protocol['temperature_K']}\nlangevinDamping {protocol['langevin_friction_per_ps']}\nlangevinHydrogen on\n"
    if pressure:
        result += f"langevinPiston on\nlangevinPistonTarget {protocol['pressure_bar']}\nlangevinPistonTemp {protocol['temperature_K']}\nlangevinPistonPeriod 100\nlangevinPistonDecay 50\n"
    else:
        result += "langevinPiston off\n"
    return result


def make(master, output):
    audit = audit_master(master)
    protocol = json.loads((master / "protocol.json").read_text())
    if protocol["lj_switching"] or protocol["lj_potential_shift"] or not protocol["lj_isotropic_tail_correction"] or protocol["production_ensemble"] != "NPT":
        raise ValueError("canonical protocol differs from this explicit native mapping")
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / "inputs" / "alanine"
    inputs.mkdir(parents=True)
    for name in ("system.prmtop", "system.rst7", "system.pdb", "protocol.json", "master-manifest.json"):
        shutil.copyfile(master / name, inputs / name)
    (inputs / "native-parameter-audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    base, box = common(protocol), cell(audit["box_A_degrees"])
    minimum, nvt, npt, production = (protocol[key] for key in ("minimization_max_iterations", "nvt_steps", "npt_steps", "production_steps"))
    (inputs / "minimize.namd").write_text(base + box + f"temperature 0\nseed {protocol['nvt_seed']}\nlangevin off\nlangevinPiston off\noutputName minimize\nminimize {minimum}\noutput minimize\n")
    (inputs / "nvt.namd").write_text(base + thermostat(protocol) + f"binCoordinates minimize.coor\nextendedSystem minimize.xsc\ntemperature {protocol['temperature_K']}\nseed {protocol['nvt_seed']}\nfirsttimestep {minimum}\noutputName nvt\nrun {nvt}\n")
    for stage, seed in (("npt", protocol["npt_seed"]), ("production", protocol["production_seed"])):
        (inputs / f"{stage}.namd").write_text(base + thermostat(protocol, pressure=True) + f"seed {seed}\n")
    singlepoint = []
    for tail in (True, False):
        name = "singlepoint-tail" if tail else "singlepoint-no-tail"
        (inputs / f"{name}.namd").write_text(common(protocol, singlepoint=True, tail=tail) + box + f"temperature 0\nseed {protocol['nvt_seed']}\nlangevin off\nlangevinPiston off\noutputName {name}\nrun 0\noutput {name}\n")
        singlepoint.append({"id": name, "steps": [{"id": name, "mode": "native", "directory": "alanine", "config": f"{name}.namd", "expected_outputs": [f"{name}.{suffix}" for suffix in ("coor", "vel", "xsc")]}]})
    def restart(name):
        return {key: name + suffix for key, suffix in (("coordinates", ".coor"), ("velocities", ".vel"), ("cell", ".xsc"))}
    stages = [{"id": name, "mode": "native", "directory": "alanine", "config": name + ".namd", "expected_outputs": [name + suffix for suffix in (".coor", ".vel", ".xsc")]} for name in ("minimize", "nvt")]
    for name, count, first, prior in (("npt", npt, minimum + nvt, "nvt"), ("production", production, minimum + nvt + npt, "npt")):
        stages.append({"id": name, "mode": "dynamics", "directory": "alanine", "config": name + ".namd", "steps": count, "segment_steps": count, "first_step": first, "output_prefix": name, "restart": restart(prior), "expected_outputs": [name + suffix for suffix in (".coor", ".vel", ".xsc")]})
    def request(jobs):
        return normalize({"schema": PARAMETER_SCHEMA, "threads": 4, "jobs": jobs, "max_wall_seconds": 14400, "max_output_bytes": 4 * 1024**3, "output_prefix": "qualification/namd/alanine", "output_destination": "platform-artifacts"})
    with tarfile.open(output / "input.tar.gz", "w:gz") as bundle:
        for path in sorted(inputs.iterdir()):
            bundle.add(path, arcname="alanine/" + path.name, recursive=False)
    (output / "request.json").write_text(json.dumps(request([{"id": "rep-1", "steps": stages}]), indent=2) + "\n")
    point = output / "singlepoint"
    point.mkdir()
    shutil.copyfile(output / "input.tar.gz", point / "input.tar.gz")
    (point / "request.json").write_text(json.dumps(request(singlepoint), indent=2) + "\n")
    provenance = {"system": "alanine-ff14sb-tip3p", "ensemble": "npt", "gpu_mode": "resident", "repetitions": 1,
                  "production_steps": production, "input_sha256": digest_file(output / "input.tar.gz"), "canonical_parameter_audit": audit,
                  "protocol_sha256": digest_file(master / "protocol.json"), "production_first_step": minimum + nvt + npt,
                  "production_relative_time_ps": [0, production * protocol["timestep_fs"] / 1000],
                  "seeds": {key: protocol[key] for key in ("nvt_seed", "npt_seed", "production_seed")},
                  "native_choices": ["Direct immutable AMBER prmtop/rst7 input; no parameter conversion",
                                     "Single-point run0 uses unconstrained coordinates and zero velocities; no-tail is a separate energy diagnostic only",
                                     "Native NVT creates Maxwell velocities at300K after minimization; RNG algorithm is not cross-engine identical",
                                     "One process per NPT/production stage preserves its RNG stream; restart commits are at full-stage boundaries",
                                     "Native isotropic Langevin piston100fs/50fs, target1.0bar; COMmotion no and group pressure recorded",
                                     "All nonbonded and PME forces every2fs, no multiple-time-step acceleration",
                                     "PME order4/grid spacing1A; actual native grid and numerical implementation must be recorded",
                                     "LJcorrection on; native analytical tail contribution compared via paired run0 diagnostics"],
                  "scientific_convergence_claimed": False}
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    (point / "provenance.json").write_text(json.dumps({**provenance, "ensemble": "singlepoint", "production_steps": 0, "repetitions": 2}, indent=2) + "\n")
    return {"fixture": str(output), "input_sha256": provenance["input_sha256"], "atoms": audit["atoms"], "active_1_4_pairs": audit["active_1_4_pairs"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(make(args.master, args.output)))
