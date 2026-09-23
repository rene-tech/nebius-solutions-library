"""Zero-optimization native PMEMD energy on the immutable canonical master."""

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

def coordinates(path):
    import numpy as np
    from netCDF4 import Dataset

    if path.read_bytes()[:3] == b"CDF":
        with Dataset(path) as data:
            return np.asarray(data.variables["coordinates"][:])
    with path.open() as handle:
        handle.readline()
        atoms = int(handle.readline().split()[0])
        values = [float(line[index:index + 12]) for line in handle for index in range(0, len(line.rstrip()), 12) if line[index:index + 12].strip()]
    return np.array(values[:atoms * 3]).reshape(atoms, 3)


def energy_terms(text):
    terms = {}
    for key in ("BOND", "ANGLE", "DIHED", "1-4 VDW", "1-4 EEL", "VDWAALS", "EEL", "HBOND", "RESTRAINT"):
        # Native EEL and 1-4 EEL are distinct energy components.
        matches = re.findall(rf"(?<!1-4 )\b{re.escape(key)}\s*=\s*([+-]?[0-9.eEdD]+)", text)
        if matches:
            terms[key] = float(matches[-1].replace("D", "E").replace("d", "e"))
    return terms


def main():
    import numpy as np

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=("cpu", "cuda-dpfp"), default="cpu")
    parser.add_argument("--lj-tail", type=int, choices=(0, 1), default=1, help="Native vdwmeth control;0 is an explicitly separate diagnostic, not production physics")
    parser.add_argument("--validate-existing", action="store_true", help="Correct validation only; keep original native files and original receipt")
    args = parser.parse_args()
    if not args.validate_existing:
        args.output.mkdir(parents=True, exist_ok=False)
        for name in ("system.prmtop", "system.rst7", "protocol.json", "master-manifest.json"):
            shutil.copy2(args.master / name, args.output / name)
        (args.output / "static.in").write_text("Canonical ff14SB/TIP3P zero-optimization single-point energy\n&cntrl\n imin=1, maxcyc=0, ncyc=0, ntx=1, irest=0,\n ntb=1, cut=10.0, ntc=1, ntf=1, ntpr=1, ntxo=2,\n/\n&ewald\n nfft1=64, nfft2=64, nfft3=64, order=4, dsum_tol=0.000001, vdwmeth=1,\n/\n")
        if args.lj_tail == 0:
            (args.output / "static.in").write_text((args.output / "static.in").read_text().replace("vdwmeth=1", "vdwmeth=0"))
    binary = "/opt/amber26/bin/pmemd" if args.backend == "cpu" else "/opt/amber26/bin/pmemd.cuda_DPFP"
    command = [binary, "-O", "-i", "static.in", "-p", "system.prmtop", "-c", "system.rst7", "-o", "static.mdout", "-r", "static.rst7", "-inf", "static.mdinfo"]
    if args.validate_existing:
        original = json.loads((args.output / "static-energy.json").read_text())
        exit_code = original["exit_code"]
        if original["mdout_sha256"] != hashlib.sha256((args.output / "static.mdout").read_bytes()).hexdigest():
            raise ValueError("native output changed since original validation")
    else:
        with (args.output / "native.log").open("w") as log:
            completed = subprocess.run(command, cwd=args.output, stdout=log, stderr=subprocess.STDOUT, timeout=120)
        exit_code = completed.returncode
    text = (args.output / "static.mdout").read_text()
    terms = energy_terms(text)
    initial = coordinates(args.output / "system.rst7")
    restart = args.output / "static.rst7"
    delta = float(np.max(np.abs(coordinates(restart) - initial))) if restart.exists() else None
    input_parity = all((args.master / name).read_bytes() == (args.output / name).read_bytes() for name in ("system.prmtop", "system.rst7"))
    status = "passed" if exit_code == 0 and len(terms) == 9 and all(math.isfinite(value) for value in terms.values()) and delta is not None and delta <= 1e-10 and input_parity else "failed"
    receipt = {"status": status, "backend": args.backend, "command": command, "exit_code": exit_code, "requested_optimization_steps": 0, "native_evaluation_count": 1, "constraints_disabled": True, "immutable_master_input_parity": input_parity, "terms_kcal_mol": terms, "sum_terms_kcal_mol": sum(terms.values()), "restart_coordinates_max_abs_difference_A": delta, "native_restart_written": restart.exists(), "input_sha256": {name: hashlib.sha256((args.output / name).read_bytes()).hexdigest() for name in ("system.prmtop", "system.rst7", "static.in")}, "mdout_sha256": hashlib.sha256((args.output / "static.mdout").read_bytes()).hexdigest(), "scientific_convergence_claimed": False}
    receipt["native_vdwmeth"] = int(re.search(r"vdwmeth=(\d+)", (args.output / "static.in").read_text()).group(1))
    if args.validate_existing:
        receipt["validation_correction"] = "PMEMD26 native field names EEL/HBOND/1-4 VDW, unchanged native artifacts"
        receipt["original_validation_sha256"] = hashlib.sha256((args.output / "static-energy.json").read_bytes()).hexdigest()
    target = "static-energy-native-fields-v2.json" if args.validate_existing else "static-energy.json"
    if (args.output / target).exists():
        raise FileExistsError("preserve original validation: " + target)
    (args.output / target).write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
    raise SystemExit(0 if status == "passed" else 1)


if __name__ == "__main__":
    main()
