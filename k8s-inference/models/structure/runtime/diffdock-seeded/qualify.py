"""Run repeated exact requests in an isolated real-GPU DiffDock candidate.

The receipt separates RDKit preprocessing repeatability from numerical GPU
repeatability. It does not assert bitwise determinism across CUDA/hardware.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
from rdkit import Chem

sys.path[:0] = ["/opt/fs2/runtime", "/opt/fs2/model/upstream"]
from adapters.diffdock import Adapter
from datasets.process_mols import generate_conformer


def saved(path: Path, value) -> str:
    raw = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def coordinates(sdf: str) -> np.ndarray:
    mol = Chem.MolFromMolBlock(sdf, sanitize=True, removeHs=True)
    if mol is None:
        raise ValueError("Cannot parse actual generated pose")
    return mol.GetConformer().GetPositions()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    raw = args.cases.read_bytes()
    cases = json.loads(gzip.decompress(raw) if args.cases.suffix == ".gz" else raw)
    preprocessing = []
    for case in cases:
        request = case["arguments"]
        conformers = []
        for _ in range(3):
            molecule = Chem.AddHs(Chem.MolFromSmiles(request["ligand"]))
            generate_conformer(molecule, random_seed=request["random_seed"])
            conformers.append(molecule.GetConformer().GetPositions())
        preprocessing.append({"case_id": case["case_id"],
            "three_repeats_bitwise_equal": all(np.array_equal(conformers[0], xyz) for xyz in conformers[1:])})
    adapter = Adapter()
    started = time.monotonic()
    adapter.load()
    loaded = time.monotonic() - started
    from utils import torus
    normalization = np.ascontiguousarray(torus.score_norm_)
    normalization_receipt = {"sha256": hashlib.sha256(normalization.tobytes()).hexdigest(),
                             "shape": list(normalization.shape), "dtype": str(normalization.dtype)}
    runs, results = [], {}
    # Interleave requests so a passing pair is not just reuse of a last result.
    for repetition in range(2):
        for case in cases:
            key = case["case_id"]
            begin = time.monotonic()
            result = adapter.infer(case["arguments"])
            if any("3D" not in pose["sdf"].splitlines()[1] for pose in result["poses"]):
                raise ValueError("Generated xyz pose is not labelled as a 3D SDF")
            elapsed = time.monotonic() - begin
            name = f"{key}-repeat{repetition + 1}.json"
            digest = saved(args.output / name, result)
            print(json.dumps({"event": "result_artifact", "filename": name, "document": result}, allow_nan=False), flush=True)
            results[key, repetition] = result
            run = {"case_id": key, "repetition": repetition + 1, "seconds": elapsed,
                   "result_file": name, "result_sha256": digest,
                   "input_sha256": hashlib.sha256(json.dumps(case["arguments"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
            runs.append(run)
            print(json.dumps({"event": "inference_completed", **run}), flush=True)
    pairs = []
    for case in cases:
        left, right = (results[case["case_id"], index] for index in (0, 1))
        if len(left["poses"]) != len(right["poses"]):
            raise ValueError("Repeated request returned different pose counts")
        delta = max(float(np.max(np.abs(coordinates(a["sdf"]) - coordinates(b["sdf"]))))
                    for a, b in zip(left["poses"], right["poses"], strict=True))
        confidence = max(abs(a["confidence"] - b["confidence"])
                         for a, b in zip(left["poses"], right["poses"], strict=True))
        pairs.append({"case_id": case["case_id"], "maximum_coordinate_difference_angstrom": delta,
                      "maximum_confidence_difference": confidence,
                      "numerical_repeatability_pass": delta <= 0.01 and confidence <= 0.001})
    receipt = {"schema": "diffdock-seed-qualification/v1", "model_load_seconds": loaded,
               "torsion_normalization": normalization_receipt,
               "gpu": subprocess.check_output(["nvidia-smi", "--query-gpu=name,uuid,driver_version", "--format=csv,noheader"], text=True).strip(),
               "adapter_identity": adapter.identity, "cases_transport_sha256": hashlib.sha256(raw).hexdigest(),
               "cases_sha256": hashlib.sha256(gzip.decompress(raw) if args.cases.suffix == ".gz" else raw).hexdigest(),
               "preprocessing": preprocessing, "runs": runs, "repeated_pairs": pairs,
               "passed": all(row["three_repeats_bitwise_equal"] for row in preprocessing) and all(row["numerical_repeatability_pass"] for row in pairs),
               "coordinate_tolerance_angstrom": 0.01, "confidence_tolerance": 0.001,
               "scientific_accuracy": "requires separate reference-pose evaluation"}
    saved(args.output / "receipt.json", receipt)
    print(json.dumps({"event": "qualification_completed", **receipt}), flush=True)
    raise SystemExit(0 if receipt["passed"] else 1)


if __name__ == "__main__":
    main()
