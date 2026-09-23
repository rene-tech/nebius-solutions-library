"""Prepare public UIUC ApoA1/STMV multi-stage fixtures without licensed downloads.

Retains source protocol and force fields; modifications are enumerated in the
provenance JSON. Performance results must disclose this output-enabled protocol.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile

from fs2_gromacs.files import digest_file, extract_inputs
from fs2_namd import PARAMETER_SCHEMA
from fs2_namd.contracts import normalize


def configuration(source, *, managed, ensemble, gpu_mode, seed):
    replacements = {"doBenchmark": "0", "doGPUres": "1" if gpu_mode == "resident" else "0",
                    "doRestart": "$fs2_restart" if managed else "0", "doMin": "0",
                    "doThermostat": "0" if ensemble == "nve" else "1",
                    "doBarostat": "1" if ensemble == "npt" else "0"}
    output = []
    for line in source.splitlines():
        found = re.match(r"\s*set\s+(\w+)\s+", line)
        if found and found[1] in replacements:
            output.append(f"set {found[1]} {replacements[found[1]]}")
        elif re.match(r"\s*(run|firsttimestep|outputName|binCoordinates|binVelocities|extendedSystem)\s+", line):
            continue
        elif re.match(r"\s*seed\s+", line):
            output.append(f"seed {seed}")
        else:
            output.append(line)
    output += ["DCDFreq 10000", "XSTFreq 1000", "restartFreq 10000"]
    if not managed:
        output += ["outputName minimize", "minimize 1000", "reinitvels 300", "output minimize"]
    if ensemble == "colvars":
        output += ["colvars on", "colvarsConfig radius.colvars"]
    return "\n".join(output) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--system", choices=("apoa1", "stmv"), required=True)
    parser.add_argument("--ensemble", choices=("nve", "npt", "colvars"), default="nve")
    parser.add_argument("--gpu-mode", choices=("resident", "offload"), default="resident")
    parser.add_argument("--steps", type=int, default=100000)
    parser.add_argument("--segment-steps", type=int, default=50000)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--screen", action="store_true", help="Omit minimization/equilibration for wrapper mechanics only, not scientific qualification")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    inputs = args.output / "inputs"
    extract_inputs(args.archive, inputs, max_bytes=1024**3)
    directory = f"{args.system}_gpu"
    system = inputs / directory
    source = (system / f"{args.system}_gpures_npt.namd").read_text()
    for ensemble in ("nve", "npt", "colvars"):
        (system / f"fs2-config-{ensemble}.namd").write_text(configuration(source, managed=True, ensemble=ensemble, gpu_mode=args.gpu_mode, seed=314159))
    (system / "fs2-config-minimize.namd").write_text(configuration(source, managed=False, ensemble="npt", gpu_mode=args.gpu_mode, seed=314159))
    # The first protein backbone group is only a technical enhanced-sampling
    # fixture. It is not a converged free-energy model or a scientific recommendation.
    (system / "radius.colvars").write_text('''colvarsTrajFrequency 1000
colvarsRestartFrequency 10000
colvar {
  name radius
  width 0.2
  gyration { atoms { atomNumbersRange 1-100 } }
}
metadynamics {
  name radius_meta
  colvars radius
  hillWeight 0.01
  hillWidth 2.0
  newHillFrequency 1000
  useGrids off
  writeHillsTrajectory on
}
''')
    def restart(prefix):
        return {"coordinates": prefix + ".coor", "velocities": prefix + ".vel", "cell": prefix + ".xsc"}
    stages = []
    if not args.screen:
        stages += [{"id": "minimize", "mode": "native", "directory": directory,
                    "config": "fs2-config-minimize.namd", "gpu_mode": args.gpu_mode,
                    "expected_outputs": ["minimize.coor", "minimize.vel", "minimize.xsc"]},
                   {"id": "equilibrate", "mode": "dynamics", "directory": directory,
                    "config": "fs2-config-npt.namd", "gpu_mode": args.gpu_mode,
                    "steps": 20000, "segment_steps": 20000, "first_step": 1000,
                    "output_prefix": "equilibrate", "restart": restart("minimize")}]
    production = {"id": "production", "mode": "dynamics", "directory": directory,
                  "config": f"fs2-config-{args.ensemble}.namd", "gpu_mode": args.gpu_mode,
                  "steps": args.steps, "segment_steps": args.segment_steps, "first_step": 0 if args.screen else 21000,
                  "output_prefix": "production", "expected_outputs": ["production.coor", "production.vel", "production.xsc"]}
    if not args.screen:
        production["restart"] = restart("equilibrate")
    if args.ensemble == "colvars" and not args.screen:
        production["initialize_colvars"] = True
    stages += [production]
    request = normalize({"schema": PARAMETER_SCHEMA, "threads": 4,
                         "jobs": [{"id": f"rep-{i+1}", "steps": stages} for i in range(args.repetitions)],
                         "max_wall_seconds": 21600, "max_output_bytes": 8 * 1024**3,
                         "output_prefix": "qualification/namd"})
    with tarfile.open(args.output / "input.tar.gz", "w:gz") as archive:
        for path in sorted(inputs.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(inputs), recursive=False)
    (args.output / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    provenance = {"source_url": f"https://www.ks.uiuc.edu/Research/namd/benchmarks/systems/{args.system}_gpu.tar.gz",
                  "source_sha256": digest_file(args.archive), "input_sha256": digest_file(args.output / "input.tar.gz"),
                  "system": args.system, "ensemble": args.ensemble, "gpu_mode": args.gpu_mode,
                  "repetitions": args.repetitions, "production_steps": args.steps, "screen_only": args.screen,
                  "changes": ["Fixed finite steps replace benchmarkTime early stop", "Explicit seed 314159",
                              "DCD every 10000, XST every 1000, restart every 10000 steps",
                              "Managed coherent segment output names and checkpoint continuation",
                              "Optional 1000-step minimization and 40000-fs NPT equilibration",
                              "Enhanced-sampling fixture uses radius metadynamics; no convergence claim"]}
    (args.output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(provenance))


if __name__ == "__main__":
    main()
