"""Canonical ff14SB/TIP3P input, no topology conversion or hidden physics edits."""

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def make(master, output, neighbor_skin=2.0):
    protocol = json.loads((master / "protocol.json").read_text())
    required = {"molecule": "ACE-ALA-NME", "force_field": "Amber ff14SB", "water_model": "TIP3P", "temperature_K": 300.0, "pressure_bar": 1.0, "timestep_fs": 2.0, "cutoff_A": 10.0, "langevin_friction_per_ps": 1.0, "nvt_steps": 50000, "npt_steps": 50000, "production_steps": 500000, "output_every_steps": 500, "constraint_tolerance": 1e-6}
    if any(protocol.get(key) != value for key, value in required.items()):
        raise ValueError("canonical master differs from the explicitly approved physical protocol")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    files = {name: (master / name).read_bytes() for name in ("system.prmtop", "system.rst7", "system.pdb", "master-manifest.json")}
    files["canonical-protocol.json"] = (master / "protocol.json").read_bytes()
    files["inspect.parmed"] = b"summary\ncheckValidity\nprintInfo POINTERS\nquit\n"
    if neighbor_skin not in (1.0, 2.0):
        raise ValueError("only documented explicit1Å/2Å pair-list margins are qualified here")
    ewald = f"&ewald\n nfft1=64, nfft2=64, nfft3=64, order=4, dsum_tol=0.00001, vdwmeth=1,\n skinnb={neighbor_skin:.1f}, skin_permit=0.5,\n/\n"
    files["minimize.in"] = ("Canonical unrestrained minimization; all master bonded terms retained\n&cntrl\n imin=1, maxcyc=5000, ncyc=2500, ntx=1, irest=0,\n ntb=1, cut=10.0, ntc=1, ntf=1, ntpr=100, ntxo=2,\n/\n" + ewald).encode()
    steps = [
        {"id": "inspect", "kind": "parmed", "input": "inspect.parmed", "topology": "system.prmtop", "coordinates": "system.rst7", "expected_outputs": ["fs2-inspect.stdout.log"]},
        {"id": "minimize", "input": "minimize.in", "task": "minimization", "topology": "system.prmtop", "coordinates": "system.rst7", "output_prefix": "minimized"},
    ]
    previous = "minimized.rst7"
    for stage, length, continued, pressure, seed in (
        ("nvt", protocol["nvt_steps"], False, False, protocol["nvt_seed"]),
        ("npt", protocol["npt_steps"], True, True, protocol["npt_seed"]),
        ("production-001", protocol["production_steps"], True, True, protocol["production_seed"]),
    ):
        # SCR is a stochastic ensemble-correct method; barostat=1 alone would
        # be plain Berendsen and is deliberately not this frozen protocol.
        native = f"""Canonical ff14SB/TIP3P {stage}; native Langevin LFMiddle {'SCR NPT' if pressure else 'NVT'}
&cntrl
 imin=0, irest={int(continued)}, ntx={5 if continued else 1}, nstlim={length}, dt=0.002,
 ntb={2 if pressure else 1}, ntp={int(pressure)}, pres0=1.0,
 barostat=1, baro_stochastic={int(pressure)}, taup=2.0, comp=44.6,
 ntt=3, gamma_ln=1.0, temp0=300.0, tempi=300.0,
 ischeme=1, ithermostat=1, therm_par=1.0,
 ntc=2, ntf=2, tol=0.000001, jfastw=0, cut=10.0,
 ig={seed}, nscm=0,
 ntpr=500, ntwx=500, ntwv=500, ntwr=10000, ioutfm=1, ntxo=2,
/
""" + ewald
        files[stage + ".in"] = native.encode()
        steps.append({"id": stage, "input": stage + ".in", "topology": "system.prmtop", "coordinates": previous, "expected_nsteps": length, "output_prefix": stage, "expected_outputs": [stage + ".nc", stage + ".mdvel"]})
        previous = stage + ".rst7"
    files["analyze.in"] = b"trajin production-001.nc\nautoimage\nrms first :1-3&!@H= out rmsd.dat\nvolume out volume.dat\nrun\n"
    steps.append({"id": "analyze", "kind": "cpptraj", "input": "analyze.in", "topology": "system.prmtop", "expected_outputs": ["rmsd.dat", "volume.dat"]})
    native_protocol = {**protocol, "fixture": "canonical-alanine-tip3p", "backend": "cuda-spfp", "ensemble": "NPT", "production_segments": 1, "trajectory_interval": 500, "timestep_ps": 0.002, "expected_atoms": 6598, "preparation": False, "immutable_master": str(master), "master_files": {name: sha(raw) for name, raw in files.items() if name in {"system.prmtop", "system.rst7", "master-manifest.json", "canonical-protocol.json"}}, "native_settings": {"ischeme": 1, "ithermostat": 1, "therm_par": 1.0, "barostat": 1, "baro_stochastic": 1, "taup_ps": 2.0, "comp_1e6_per_bar": 44.6, "nfft": [64, 64, 64], "order": 4, "vdwmeth": 1, "ntwv": 500, "nscm": 0}, "barostat_method": "stochastic cell rescaling, not plain Berendsen", "pressure_observable": "native molecular-virial pressure, not Monte Carlo placeholder PRESS=0", "manual": "https://ambermd.org/doc12/Amber26.pdf#page=446", "exact_stochastic_continuation_claimed": False, "scientific_convergence_claimed": False}
    native_protocol["native_settings"].update(skinnb_A=neighbor_skin, skin_permit=0.5)
    native_protocol["neighbor_margin_provenance"] = "Explicit2Å list margin avoids fixed CUDA neighbor-cell exhaustion during density equilibration; physical10Å cutoff unchanged,strict half-skin rebuild threshold,unsafe small-box override disabled. Failed native-default1Å attempt retained separately."
    files["protocol.json"] = json.dumps(native_protocol, indent=2).encode() + b"\n"
    request = {"schema": "fs2-serve.nebius.ai/amber-workflow-request/v1", "backend": "cuda-spfp", "threads": 1, "max_wall_seconds": 7200, "max_output_bytes": 1024**3, "jobs": [{"id": "canonical-alanine-tip3p", "steps": steps}]}
    (output / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    with tarfile.open(output / "input.tar.gz", "w:gz") as archive:
        for name, raw in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(raw), 0o644, 0
            archive.addfile(info, io.BytesIO(raw))
    manifest = {"input_sha256": sha((output / "input.tar.gz").read_bytes()), "request_sha256": sha((output / "request.json").read_bytes()), "files": [{"path": name, "sha256": sha(raw), "size_bytes": len(raw)} for name, raw in sorted(files.items())], "protocol": native_protocol}
    (output / "fixture-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--neighbor-skin", type=float, choices=(1.0, 2.0), default=2.0)
    args = parser.parse_args()
    print(json.dumps(make(args.master, args.output, args.neighbor_skin)))


if __name__ == "__main__":
    main()
