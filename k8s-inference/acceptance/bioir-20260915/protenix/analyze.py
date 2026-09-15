#!/usr/bin/env python3
"""Summarize retained attempts and per-chain CA-lDDT against public structures.

This descriptive small cohort is not a scientific equivalence/non-inferiority
study. Homodimer chain fold quality does not establish interface accuracy.
"""
from __future__ import annotations

from collections import defaultdict
import io
import json
from pathlib import Path
import statistics

import numpy as np
from Bio.Align import PairwiseAligner
from Bio.PDB import MMCIFParser, PDBParser
from Bio.SeqUtils import seq1


HERE = Path(__file__).resolve().parent


def residues(chain):
    return [(seq1(residue.resname), np.array(residue["CA"].coord, dtype=float))
            for residue in chain.get_residues() if "CA" in residue and residue.id[0] == " "]


def compare_chain(prediction, reference):
    align = PairwiseAligner()
    align.match_score, align.mismatch_score = 2, -1
    align.open_gap_score, align.extend_gap_score = -5, -0.5
    best = align.align("".join(x[0] for x in reference), "".join(x[0] for x in prediction))[0]
    pairs = [(i, j) for (a, b), (c, d) in zip(best.aligned[0], best.aligned[1])
             for i, j in zip(range(a, b), range(c, d)) if reference[i][0] == prediction[j][0]]
    truth = np.array([reference[i][1] for i, _ in pairs])
    pred = np.array([prediction[j][1] for _, j in pairs])
    truth_dist = np.linalg.norm(truth[:, None] - truth[None, :], axis=-1)
    pred_dist = np.linalg.norm(pred[:, None] - pred[None, :], axis=-1)
    selected = (truth_dist < 15) & (truth_dist > 0)
    errors = np.abs(truth_dist - pred_dist)[selected]
    left, right = pred - pred.mean(axis=0), truth - truth.mean(axis=0)
    u, _, vh = np.linalg.svd(left.T @ right)
    correction = np.diag([1.0, 1.0, np.linalg.det(u @ vh)])
    aligned = left @ (u @ correction @ vh)
    return {"matched_residues": len(pairs), "prediction_residues": len(prediction),
            "ca_lddt": float(np.mean([(errors < limit).mean() for limit in (0.5, 1, 2, 4)])),
            "aligned_ca_rmsd_angstrom": float(np.sqrt(np.mean(np.sum((aligned - right) ** 2, axis=1))))}


def markers(text):
    rows = []
    for line in text.splitlines():
        for prefix in ("FS2_STARTUP ", "BIOIR_PHASES "):
            if prefix in line:
                try:
                    rows.append(json.loads(line.split(prefix, 1)[1]))
                except json.JSONDecodeError:
                    pass
    return rows


def main():
    structures = json.loads((HERE.parent / "coverage/fixtures/structures.json").read_text())["structures"]
    references = {}
    for code in ("1UBQ", "1LYZ"):
        structure = PDBParser(QUIET=True).get_structure(code, io.StringIO(structures[code]["pdb"]))
        references[code] = residues(next(structure.get_chains()))
    groups = defaultdict(list)
    all_attempts = []
    for file in sorted((HERE / "raw").glob("*/attempts.jsonl")):
        for line in file.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("phase") == "cpu-prepare":
                all_attempts.append({**row, "evidence": str(file.relative_to(HERE))})
                continue
            row["evidence"] = str(file.relative_to(HERE))
            row["quality_against_reference"] = []
            for artifact in row.get("artifacts", []):
                if not artifact["path"].endswith(".cif"):
                    continue
                cif = file.parent / artifact["path"]
                parsed = MMCIFParser(QUIET=True).get_structure("prediction", cif)
                ref = references["1UBQ" if row["case"].startswith("ubiquitin") else "1LYZ"]
                for chain in parsed.get_chains():
                    coordinates = residues(chain)
                    if coordinates:
                        row["quality_against_reference"].append({
                            "chain": chain.id, "reference": "1UBQ" if len(ref) == 76 else "1LYZ",
                            **compare_chain(coordinates, ref),
                        })
            row["expected_sequence_match"] = all(
                score["matched_residues"] == score["prediction_residues"] == (76 if row["case"].startswith("ubiquitin") else 129)
                for score in row["quality_against_reference"]
            ) if row["quality_against_reference"] else None
            if row.get("log"):
                log = file.parent / row["log"]
            else:
                log = file.parent / row["case"] / f"r{row['repetition']:02d}/request.log"
            events = markers(log.read_text()) if log.is_file() else []
            forward = []
            initialized = []
            starts = {}
            for event in events:
                phase = event.get("phase")
                if "forward_seconds" in event:
                    forward.append(event["forward_seconds"])
                if phase in {"first_compute_start", "model_initialization_start"}:
                    starts[phase] = event["monotonic_seconds"]
                if phase == "first_compute_complete" and "first_compute_start" in starts:
                    forward.append(event["monotonic_seconds"] - starts.pop("first_compute_start"))
                if phase == "model_ready" and "model_initialization_start" in starts:
                    initialized.append(event["monotonic_seconds"] - starts.pop("model_initialization_start"))
            row["forward_seconds"] = sum(forward) if forward else None
            row["model_initialization_seconds"] = sum(initialized) if initialized else None
            all_attempts.append(row)
            groups[(row["variant"], row["case"])].append(row)
    summaries = []
    for (variant, case), rows in sorted(groups.items()):
        valid = [row for row in rows if row["status"] == "passed"]
        walls = [row["wall_seconds"] for row in valid]
        forwards = [row["forward_seconds"] for row in valid if row["forward_seconds"] is not None]
        scores = [score["ca_lddt"] for row in valid for score in row["quality_against_reference"]]
        first = [row["wall_seconds"] for row in valid if row["repetition"] == 1]
        warm = [row["wall_seconds"] for row in valid if row["repetition"] > 1]
        warm_forward = [row["forward_seconds"] for row in valid
                        if row["repetition"] > 1 and row["forward_seconds"] is not None]
        summaries.append({
            "variant": variant, "case": case, "attempts": len(rows), "valid": len(valid),
            "wall_seconds": walls, "median_wall_seconds": statistics.median(walls) if walls else None,
            "forward_seconds": forwards,
            "median_forward_seconds": statistics.median(forwards) if forwards else None,
            "first_shape_request_seconds": first,
            "subsequent_requests": len(warm),
            "median_subsequent_wall_seconds": statistics.median(warm) if warm else None,
            "median_subsequent_forward_seconds": statistics.median(warm_forward) if warm_forward else None,
            "per_chain_ca_lddt": scores,
            "median_chain_ca_lddt": statistics.median(scores) if scores else None,
            "serial_request_window_gpu_seconds_per_success":
                sum(row["wall_seconds"] for row in rows) / len(valid) if valid else None,
        })
    report = {
        "scope": "Public 76/129/258-residue fixtures; 10 cycles/200 steps/one sample; no MSA/templates/RNA",
        "quality_metric": "Sequence-aligned per-chain CA-lDDT,15A,thresholds0.5/1/2/4A; not all-atom lDDT or DockQ",
        "limitations": [
            "Prototype cohorts use a different H100/driver and must not enter speedup ratios.",
            "No p95/p99 claim from three repetitions; first shape and steady timing must be shown separately.",
            "Request-window GPU-seconds exclude pod provisioning/loading outside requests and operator think time.",
            "Per-chain fold scores do not validate the artificial homodimer interface or prove scientific equivalence.",
        ],
        "summaries": summaries, "attempts": all_attempts,
    }
    (HERE / "analysis.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
