#!/usr/bin/env python3
"""Invoke unchanged Protenix stage validation, then assert chain sequence/graphs."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--cases", default="ubiquitin-76,lysozyme-129")
parser.add_argument("--label", required=True)
args = parser.parse_args()
os.setgid(10001)
os.setuid(10001)
base = Path("/mnt/fs2-scientific/baseline")
if not (base / "ubiquitin-76/prediction-command.json").is_file():
    subprocess.run([sys.executable, "/evaluation/run_baseline.py", "--marker-template", "/evaluation/marker.json", "--directory", str(base), "--prepare-only"], check=True, stdout=sys.stderr)
output = Path("/mnt/fs2-scientific/results") / args.label
command = [sys.executable, "/evaluation/run_prepared.py", "--fixtures", str(base), "--directory", str(output), "--variant", args.label, "--repetitions", "1", "--stop-on-failure"]
for case in args.cases.split(","):
    command.extend(["--case", case])
subprocess.run(command, check=True, stdout=sys.stderr)
from Bio.PDB import MMCIFParser
from Bio.SeqUtils import seq1
from run_baseline import UBIQUITIN, LYSOZYME
for line in (output / "attempts.jsonl").read_text().splitlines():
    row = json.loads(line)
    expected = UBIQUITIN if row["case"].startswith("ubiquitin") else LYSOZYME
    for path in (output / row["case"]).rglob("*.cif"):
        model = MMCIFParser(QUIET=True).get_structure("prediction", path)
        chains = ["".join(seq1(r.resname) for r in chain if "CA" in r and r.id[0] == " ") for chain in model.get_chains()]
        assert chains == [expected] * (2 if "homodimer" in row["case"] else 1), chains
    log = (output / row["case"] / "r01/request.log").read_text()
    phases = [json.loads(line.split("BIOIR_PHASES ", 1)[1]) for line in log.splitlines() if "BIOIR_PHASES " in line]
    assert phases and all(p["sampling_rng"] == "caller-global-stream" for p in phases)
    assert all(p["cycles"] == 10 and p["steps"] == 200 and p["samples"] == 1 for p in phases)
    row["sequence_valid"] = True
    row["bioir_phases"] = phases
    row["graph_verified"] = any(s["captured"] and "GRAPH_VERIFIED" in s["state"] for p in phases for module in p["graph_state"] for s in module["states"])
    assert row["graph_verified"], "actual graph replay state unavailable"
    row["scientific_parity"] = "not-qualified; model-lane reference-quality regression remains"
    print(json.dumps(row), flush=True)
