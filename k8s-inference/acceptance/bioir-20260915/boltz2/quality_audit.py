"""Offline audit of chain identity, sequence and reference-aligned CA geometry."""
import argparse
import io
import json
from pathlib import Path

import numpy as np
from Bio.Align import PairwiseAligner
from Bio.PDB import MMCIFParser, PDBParser
from Bio.SeqUtils import seq1


def chains(structure):
    return {chain.id: [(seq1(res.resname), np.asarray(res["CA"].coord, dtype=float))
                       for res in chain if res.id[0] == " " and "CA" in res]
            for chain in structure.get_chains()}


def audit(payload, fixture, truth_path):
    pred = chains(MMCIFParser(QUIET=True).get_structure("pred", io.StringIO(payload)))
    parser = MMCIFParser(QUIET=True) if truth_path.suffix == ".cif" else PDBParser(QUIET=True)
    truth = chains(parser.get_structure("truth", str(truth_path)))
    expected = {p["id"]: p["sequence"] for p in fixture["polymers"]}
    sequences = {key: "".join(r[0] for r in value) for key, value in pred.items()}
    mapping, used = {}, set()
    for key, seq in expected.items():
        matches = [candidate for candidate, value in sequences.items() if value == seq and candidate not in used]
        selected = key if key in matches else (matches[0] if matches else None)
        mapping[key] = selected
        used.add(selected)
    result = {"expected_chain_ids": list(expected), "output_chain_ids": list(pred),
              "chain_ids_preserved": set(expected) == set(pred),
              "sequences_preserved": None not in mapping.values() and len(used) == len(pred),
              "sequence_mapping": mapping}
    if not result["sequences_preserved"]:
        return result
    reference_coordinates, predicted_coordinates = [], []
    aligner = PairwiseAligner()
    aligner.match_score, aligner.mismatch_score = 2, -1
    aligner.open_gap_score, aligner.extend_gap_score = -5, -0.5
    for key, candidate in mapping.items():
        reference = truth.get(key)
        if reference is None and len(truth) == 1:
            reference = next(iter(truth.values()))
        if reference is None:
            result["reference_error"] = "Missing reference chain " + key
            return result
        output = pred[candidate]
        alignment = aligner.align("".join(r[0] for r in reference), sequences[candidate])[0]
        for (a, b), (c, d) in zip(*alignment.aligned):
            for i, j in zip(range(a, b), range(c, d)):
                if reference[i][0] == output[j][0]:
                    reference_coordinates.append(reference[i][1])
                    predicted_coordinates.append(output[j][1])
    ref, out = np.asarray(reference_coordinates), np.asarray(predicted_coordinates)
    rd = np.linalg.norm(ref[:, None] - ref[None, :], axis=-1)
    od = np.linalg.norm(out[:, None] - out[None, :], axis=-1)
    errors = np.abs(rd - od)[(rd > 0) & (rd < 15)]
    result["chain_aware_ca_lddt"] = float(np.mean([(errors < t).mean() for t in (0.5, 1, 2, 4)]))
    result["matched_reference_residues"] = len(ref)
    result["not_dockq"] = True
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = []
    seen = set()
    for path in sorted(args.evidence.rglob("attempts.jsonl")):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            key = (row["variant"], row["attempt"], row["started_unix"])
            if key in seen or row["status"] != "valid":
                continue
            seen.add(key)
            response = path.parent / f'{row["attempt"]:03d}-{row["fixture"]}.response.json'
            fixture = json.loads((args.fixtures / (row["fixture"] + ".json")).read_text())
            truth = args.fixtures / (row["fixture"] + ".pdb")
            if not truth.exists():
                truth = truth.with_suffix(".cif")
            for index, structure in enumerate(json.loads(response.read_text())["structures"]):
                result.append({"variant": row["variant"], "attempt": row["attempt"], "sample": index,
                               "fixture": row["fixture"], **audit(structure["structure"], fixture, truth)})
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"samples_audited": len(result), "chain_id_failures": sum(not r["chain_ids_preserved"] for r in result),
                      "sequence_failures": sum(not r["sequences_preserved"] for r in result)}))
