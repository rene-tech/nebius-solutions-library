"""Full ff19SB/OPC preparation-to-analysis fixture with explicit native physics."""

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path


def make(args):
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    if args.steps % 10000:
        raise ValueError("production length must divide two stages and5000-step output cadence")
    leap = args.leap.read_bytes()
    files = {"alanine.leap": leap, "inspect.parmed": b"summary\ncheckValidity\nprintInfo POINTERS\nquit\n"}
    top = "alanine-opc.prmtop"
    files["minimize.in"] = b"""ff19SB/OPC restrained minimization, explicit preparation protocol
&cntrl
 imin=1, maxcyc=1000, ncyc=500, ntx=1, irest=0,
 ntb=1, cut=8.0, ntc=1, ntf=1,
 ntr=1, restraint_wt=2.0, restraintmask='@CA',
 ntpr=100, ntxo=2,
/
"""
    files["heat.in"] = b"""NVT heating10K to300K over100ps, native temperature schedule
&cntrl
 imin=0, irest=0, ntx=1, nstlim=50000, dt=0.002,
 ntb=1, ntp=0, ntt=3, gamma_ln=1.0, tempi=10.0, temp0=300.0,
 ntc=2, ntf=2, tol=0.000001, cut=8.0, ig=71277, nscm=0,
 ntpr=1000, ntwx=5000, ntwr=10000, ioutfm=1, ntxo=2, nmropt=1,
/
&wt type='TEMP0', istep1=0, istep2=50000, value1=10.0, value2=300.0 /
&wt type='END' /
"""
    def npt(length, seed):
        return f"""Native ff19SB/OPC Langevin/Monte Carlo NPT,300K and1bar
&cntrl
 imin=0, irest=1, ntx=5, nstlim={length}, dt=0.002,
 ntb=2, ntp=1, pres0=1.0, barostat=2, mcbarint=100, taup=2.0,
 ntt=3, gamma_ln=1.0, temp0=300.0,
 ntc=2, ntf=2, tol=0.000001, cut=8.0, ig={seed}, nscm=0,
 ntpr=1000, ntwx=5000, ntwr=10000, ioutfm=1, ntxo=2,
/
""".encode()
    files["equilibrate.in"] = npt(50000, 71278)
    steps = [
        {"id": "prepare", "kind": "tleap", "input": "alanine.leap", "expected_outputs": [top, "alanine-opc.inpcrd", "alanine-opc.pdb"]},
        {"id": "inspect", "kind": "parmed", "input": "inspect.parmed", "topology": top, "coordinates": "alanine-opc.inpcrd", "expected_outputs": ["fs2-inspect.stdout.log"]},
        {"id": "minimize", "input": "minimize.in", "task": "minimization", "topology": top, "coordinates": "alanine-opc.inpcrd", "reference": "alanine-opc.inpcrd", "output_prefix": "minimized"},
        {"id": "heat", "input": "heat.in", "topology": top, "coordinates": "minimized.rst7", "expected_nsteps": 50000, "output_prefix": "heated", "expected_outputs": ["heated.nc"]},
        {"id": "equilibrate", "input": "equilibrate.in", "topology": top, "coordinates": "heated.rst7", "expected_nsteps": 50000, "output_prefix": "equilibrated", "expected_outputs": ["equilibrated.nc"]},
    ]
    previous = "equilibrated.rst7"
    for index in range(2):
        stage = f"production-{index + 1:03d}"
        files[stage + ".in"] = npt(args.steps // 2, 71279 + index)
        steps.append({"id": stage, "input": stage + ".in", "topology": top, "coordinates": previous, "expected_nsteps": args.steps // 2, "output_prefix": stage, "expected_outputs": [stage + ".nc"]})
        previous = stage + ".rst7"
    files["analyze.in"] = b"trajin production-001.nc\ntrajin production-002.nc\nrms first :1-3&!@H= out rmsd.dat\nvolume out volume.dat\nrun\n"
    steps.append({"id": "analyze", "kind": "cpptraj", "input": "analyze.in", "topology": top, "expected_outputs": ["rmsd.dat", "volume.dat"]})
    protocol = {"fixture": "alanine-opc-full", "backend": "cuda-spfp", "production_steps": args.steps, "production_segments": 2, "trajectory_interval": 5000, "timestep_ps": 0.002, "ensemble": "NPT", "preparation": True, "expected_atoms": 2646, "sources": [{"source": str(args.leap), "sha256": hashlib.sha256(leap).hexdigest()}], "scientific_convergence_claimed": False, "exact_stochastic_continuation_claimed": False, "notes": "Actual tleap ff19SB/OPC+ParmEd, restrained minimization,100ps heating,100ps density equilibration,explicit stage seeds,PMEMD CUDA SPFP production,CPPTRAJ RMSD/volume. Bounded qualification is not equilibrium or molecular convergence proof."}
    files["protocol.json"] = json.dumps(protocol, indent=2).encode() + b"\n"
    request = {"schema": "fs2-serve.nebius.ai/amber-workflow-request/v1", "jobs": [{"id": "alanine-opc-full", "steps": steps}], "backend": "cuda-spfp", "threads": 1, "max_wall_seconds": 7200, "max_output_bytes": 1024**3}
    (args.output / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    with tarfile.open(args.output / "input.tar.gz", "w:gz") as archive:
        for name, raw in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(raw), 0o644, 0
            archive.addfile(info, io.BytesIO(raw))
    manifest = {"input_sha256": hashlib.sha256((args.output / "input.tar.gz").read_bytes()).hexdigest(), "request_sha256": hashlib.sha256((args.output / "request.json").read_bytes()).hexdigest(), "files": [{"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)} for name, raw in sorted(files.items())], "protocol": protocol}
    (args.output / "fixture-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leap", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=1000000)
    args = parser.parse_args()
    print(json.dumps(make(args)))


if __name__ == "__main__":
    main()
