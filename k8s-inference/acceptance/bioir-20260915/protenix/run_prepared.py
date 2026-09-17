#!/usr/bin/env python3
"""Benchmark prepared public inputs through the unchanged validation wrapper."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--start-repetition", type=int, default=1)
    parser.add_argument("--case", action="append")
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument("--wrapper", help="optional resident upstream CLI bridge")
    parser.add_argument("--seeds", help="native comma-separated seed list")
    parser.add_argument("--sample-count", type=int)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    cases = sorted(path.parent for path in args.fixtures.glob("*/prediction-command.json"))
    if args.case:
        cases = [case for case in cases if case.name in args.case]
    if not cases:
        raise ValueError("no prepared public fixtures selected")
    for repetition in range(args.start_repetition, args.start_repetition + args.repetitions):
        for case in cases:
            output = args.directory / case.name / f"r{repetition:02d}"
            output.mkdir(parents=True, exist_ok=False)
            command = json.loads((case / "prediction-command.json").read_text())
            for flag, value in (("--seeds", args.seeds), ("--sample-count", args.sample_count)):
                if value is not None:
                    command[command.index(flag) + 1] = str(value)
            expected_samples = (len(command[command.index("--seeds") + 1].split(","))
                                * int(command[command.index("--sample-count") + 1]))
            if args.wrapper:
                command[:1] = ["/opt/protenix-venv/bin/python", args.wrapper]
            command.extend(["--output-dir", str(output)])
            started = time.monotonic()
            row = {"model_id": "protenix-v2", "variant": args.variant,
                   "case": case.name, "repetition": repetition,
                   "created_at": datetime.now(timezone.utc).isoformat(), "command": command,
                   "input_sha256": hashlib.sha256((case / "input.json").read_bytes()).hexdigest()}
            try:
                done = subprocess.run(command, capture_output=True, text=True,
                                      timeout=1200, check=False, env=dict(os.environ))
                (output / "request.log").write_text(done.stdout + "\nSTDERR\n" + done.stderr)
                row.update(return_code=done.returncode, status="passed" if done.returncode == 0 else "failed")
                if done.returncode == 0:
                    from Bio.PDB.MMCIF2Dict import MMCIF2Dict

                    structures = list(output.rglob("*.cif"))
                    assert len(structures) == expected_samples, (
                        f"expected {expected_samples} structures, got {len(structures)}")
                    atom_counts = []
                    for structure in structures:
                        atoms = MMCIF2Dict(str(structure))
                        coordinates = [float(value) for field in ("x", "y", "z")
                                       for value in atoms["_atom_site.Cartn_" + field]]
                        assert coordinates and all(math.isfinite(value) for value in coordinates)
                        atom_counts.append(len(atoms["_atom_site.Cartn_x"]))
                    row["quality"] = {"finite_coordinates": True,
                                      "atom_counts": atom_counts,
                                      "structures": len(structures),
                                      "scientific_equivalence": "pending-paired-analysis"}
            except Exception as error:
                row.update(status="failed", error=repr(error))
            row["wall_seconds"] = time.monotonic() - started
            row["artifacts"] = [
                {"path": str(path.relative_to(args.directory)), "bytes": path.stat().st_size,
                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in sorted(output.rglob("*")) if path.is_file()
            ]
            with (args.directory / "attempts.jsonl").open("a") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            print(json.dumps(row, sort_keys=True), flush=True)
            if row["status"] != "passed" and args.stop_on_failure:
                raise SystemExit(1)


if __name__ == "__main__":
    main()
