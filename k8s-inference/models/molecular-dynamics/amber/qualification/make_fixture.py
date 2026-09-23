"""Create PRIVATE sustained workflows from licensed installed upstream assets.

Do not check the generated bundle into Git or redistribute it. This generator
records every source asset hash and the explicit protocol changes relative to
upstream short regression fixtures. No free-energy convergence is implied.
"""

import argparse
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def protocol(case, steps, *, restart=True, trajectory=1000):
    if case == "myoglobin-gb8":
        return f"""Sustained myoglobin GB8 NVE, explicit fixed protocol
&cntrl
 imin=0, irest={1 if restart else 0}, ntx={5 if restart else 1},
 nstlim={steps}, dt=0.002, ntb=0, igb=8,
 ntc=2, ntf=2, tol=0.000001,
 cut=9999.0, rgbmax=15.0, ntt=0, nscm=0, ig=71277,
 ntpr=1000, ntwx={trajectory}, ntwr=10000, ioutfm=1, ntxo=2,
/
"""
    npt = case == "dhfr-npt"
    return f"""Sustained DHFR {'NPT Langevin/Monte Carlo' if npt else 'NVE'} explicit-solvent protocol
&cntrl
 imin=0, irest={1 if restart else 0}, ntx={5 if restart else 1},
 nstlim={steps}, dt={0.002 if npt else 0.001},
 ntb={2 if npt else 1}, ntp={1 if npt else 0},
 ntt={3 if npt else 0}, gamma_ln={1.0 if npt else 0.0}, temp0=300.0, tempi=300.0,
 barostat={2 if npt else 1}, mcbarint=100, taup=2.0,
 ntc=2, ntf=2, tol=0.0000001, cut=8.0,
 nscm=0, ig=71277,
 ntpr=1000, ntwx={trajectory}, ntwr=10000, ioutfm=1, ntxo=2,
/
&ewald
 nfft1=72, nfft2=64, nfft3=60, netfrc=0,
/
"""


def make(args):
    if args.steps % args.segments or (args.steps // args.segments) % 1000:
        raise ValueError("production segments must divide steps and contain whole1000-step trajectory intervals")
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    files, provenance = {}, []
    if args.mbar_neighbors and args.case != "complex-ti-mbar":
        raise ValueError("nearby-state MBAR is an explicit complex TI fixture only")
    label = args.case + "-nearby" if args.mbar_neighbors else args.case
    if args.case.startswith("dhfr-"):
        source, top, coordinates = args.assets / "cuda/dhfr", "prmtop", "md12.x"
    elif args.case == "myoglobin-gb8":
        source, top, coordinates = args.assets / "cuda/myoglobin", "prmtop.igb78", "inpcrd"
    else:
        source, top, coordinates = args.assets / "cuda/gti/complex", "prmtop", "inpcrd"
    for name, target in ((top, "system.prmtop"), (coordinates, "start.rst7")):
        raw = (source / name).read_bytes()
        files[target] = raw
        provenance.append({"source": str(source / name), "sha256": sha(raw), "target": target})
    steps = []
    backend = args.backend or ("cuda-dpfp" if args.case == "complex-ti-mbar" else "cuda-spfp")
    if args.case == "complex-ti-mbar":
        native = (source / "Run.SC_NVT_MBAR").read_text()
        match = re.search(r"cat > mdin <<EOF\n(.*?)\nEOF", native, re.S)
        if match is None:
            raise ValueError("upstream native MDIN block not found")
        ti = match.group(1) + "\n"
        if args.mbar_neighbors:
            ti, count = re.subn(r"(?m)^\s*mbar_lambda\s*=.*$", "  mbar_lambda = 0.2, 0.3, 0.4,", ti)
            ti, count_states = re.subn(r"(?m)^\s*mbar_states\s*=.*$", "  mbar_states = 3,", ti)
            if (count, count_states) != (1, 1):
                raise ValueError("upstream MBAR state list/count is not explicit")
        provenance.append({"source": str(source / "Run.SC_NVT_MBAR"), "sha256": sha(native.encode()), "selection": "native MDIN heredoc"})
        def mdin(length, continued, seed):
            value = ti
            replacements = {"nstlim": length, "ntpr": 1000, "ntwx": 1000, "ntwr": 10000, "ioutfm": 1, "ntxo": 2, "irest": 1 if continued else 0, "bar_intervall": 100, "ig": seed}
            for key, item in replacements.items():
                value, count = re.subn(rf"(?im)^(\s*{key}\s*=\s*)[^,\n]+", lambda m: m.group(1) + str(item), value)
                if count != 1:
                    raise ValueError(f"native TI field {key} not found exactly once")
            return value
    else:
        def mdin(length, continued, seed):
            return protocol(args.case, length, restart=continued).replace("ig=71277", f"ig={seed}")
    if args.prepare:
        if not args.case.startswith("dhfr-"):
            raise ValueError("preparation lane is the explicit DHFR workflow")
        files["minimize.in"] = b"""Restrained DHFR minimization before explicit equilibration
&cntrl
 imin=1, maxcyc=500, ncyc=250, ntx=1, irest=0,
 ntb=1, cut=8.0, ntc=1, ntf=1,
 ntr=1, restraint_wt=2.0, restraintmask='@CA',
 ntpr=50, ntxo=2,
/
"""
        steps.append({"id": "minimize", "input": "minimize.in", "task": "minimization", "topology": "system.prmtop", "coordinates": "start.rst7", "reference": "start.rst7", "output_prefix": "minimized"})
        files["equilibrate.in"] = protocol("dhfr-npt", 10000, restart=False).replace("ntb=2, ntp=1", "ntb=1, ntp=0").encode()
        steps.append({"id": "equilibrate", "input": "equilibrate.in", "topology": "system.prmtop", "coordinates": "minimized.rst7", "expected_nsteps": 10000, "output_prefix": "equilibrated", "expected_outputs": ["equilibrated.nc"]})
        previous = "equilibrated.rst7"
    else:
        warmup = 2000
        files["warmup.in"] = mdin(warmup, args.case != "complex-ti-mbar", 71277).encode()
        steps.append({"id": "warmup", "input": "warmup.in", "topology": "system.prmtop", "coordinates": "start.rst7", "expected_nsteps": warmup, "output_prefix": "warmed", "expected_outputs": ["warmed.nc"]})
        previous = "warmed.rst7"
    for index in range(args.segments):
        stage = f"production-{index + 1:03d}"
        length = args.steps // args.segments
        # Explicit stage seeds are independent; no hidden random-stream claim.
        files[stage + ".in"] = mdin(length, True, 71278 + index).encode()
        steps.append({"id": stage, "input": stage + ".in", "topology": "system.prmtop", "coordinates": previous, "expected_nsteps": length, "output_prefix": stage, "expected_outputs": [stage + ".nc"]})
        previous = stage + ".rst7"
    trajectories = [step["output_prefix"] + ".nc" for step in steps if step["id"].startswith("production-")]
    files["analyze.in"] = ("\n".join("trajin " + name for name in trajectories) + "\nrms first !@H= out rmsd.dat\nrun\n").encode()
    steps.append({"id": "analyze", "kind": "cpptraj", "input": "analyze.in", "topology": "system.prmtop", "expected_outputs": ["rmsd.dat"]})
    description = {"fixture": args.case, "backend": backend, "production_steps": args.steps, "production_segments": args.segments, "trajectory_interval": 1000, "timestep_ps": 0.001 if args.case in {"dhfr-nve", "complex-ti-mbar"} else 0.002, "ensemble": "NPT" if args.case == "dhfr-npt" else "NVT-TI" if args.case == "complex-ti-mbar" else "NVE", "preparation": args.prepare, "sources": provenance, "changes_from_upstream_short_regression": "longer explicit stage lengths, output cadence, clean-stage restarts; DHFR NPT uses explicit Langevin/Monte Carlo; TI lambda0.30 with11 MBAR states and explicit100-step MBAR output; full native MDIN files are the authoritative protocol", "scientific_convergence_claimed": False, "exact_stochastic_continuation_claimed": False}
    description["fixture"] = label
    if args.mbar_neighbors:
        description["changes_from_upstream_short_regression"] += "; separately named nearby-state control uses only MBAR lambda0.2/0.3/0.4 around sampled0.3; original full0..1-grid failure is retained, not reclassified"
        description["mbar_lambdas"] = [0.2, 0.3, 0.4]
    files["protocol.json"] = json.dumps(description, indent=2).encode() + b"\n"
    request = {"schema": "fs2-serve.nebius.ai/amber-workflow-request/v1", "backend": backend, "threads": 1, "max_wall_seconds": 7200, "max_output_bytes": 4 * 1024**3, "jobs": [{"id": label, "steps": steps}]}
    (args.output / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    with tarfile.open(args.output / "input.tar.gz", "w:gz") as archive:
        for name, raw in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(raw), 0o644, 0
            archive.addfile(info, io.BytesIO(raw))
    manifest = {"input_sha256": sha((args.output / "input.tar.gz").read_bytes()), "request_sha256": sha((args.output / "request.json").read_bytes()), "files": [{"path": name, "sha256": sha(raw), "size_bytes": len(raw)} for name, raw in sorted(files.items())], "private_licensed_assets": True, "protocol": description}
    (args.output / "fixture-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=("dhfr-nve", "dhfr-npt", "myoglobin-gb8", "complex-ti-mbar"), required=True)
    parser.add_argument("--steps", type=int, default=100000)
    parser.add_argument("--segments", type=int, default=2)
    parser.add_argument("--backend", choices=("cpu", "cuda-spfp", "cuda-dpfp"))
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--mbar-neighbors", action="store_true", help="New explicitly named3-nearby-state analysis, not a repair/relabeling of the failed11-state test")
    args = parser.parse_args()
    print(json.dumps(make(args)))


if __name__ == "__main__":
    main()
