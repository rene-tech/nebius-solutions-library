"""In-pod benchmark: preserve every attempt, HTTP response and CA-lDDT score."""
import argparse
import hashlib
import io
import json
import math
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from Bio.PDB import MMCIFParser, PDBParser
from Bio.Align import PairwiseAligner
from Bio.SeqUtils import seq1


def ca_residues(structure):
    return [(seq1(r.resname), np.array(r["CA"].coord, dtype=float))
            for r in structure.get_residues() if "CA" in r and r.id[0] == " "]


def quality(cif, reference, expected):
    predicted = MMCIFParser(QUIET=True).get_structure("prediction", io.StringIO(cif))
    atoms = list(predicted.get_atoms())
    xyz = np.array([a.coord for a in atoms])
    assert len(atoms) > 0 and np.isfinite(xyz).all(), "empty/nonfinite coordinates"
    pred = ca_residues(predicted)
    assert len(pred) == expected, f"expected {expected} residues, got {len(pred)}"
    reference_parser = MMCIFParser(QUIET=True) if str(reference).endswith(".cif") else PDBParser(QUIET=True)
    truth = ca_residues(reference_parser.get_structure("reference", reference))
    aligner = PairwiseAligner()
    aligner.match_score, aligner.mismatch_score = 2, -1
    aligner.open_gap_score, aligner.extend_gap_score = -5, -0.5
    aln = aligner.align("".join(x[0] for x in truth), "".join(x[0] for x in pred))[0]
    pairs = [(i, j) for (a, b), (c, d) in zip(aln.aligned[0], aln.aligned[1])
             for i, j in zip(range(a, b), range(c, d)) if truth[i][0] == pred[j][0]]
    ref = np.array([truth[i][1] for i, _ in pairs])
    out = np.array([pred[j][1] for _, j in pairs])
    rd = np.linalg.norm(ref[:, None] - ref[None, :], axis=-1)
    od = np.linalg.norm(out[:, None] - out[None, :], axis=-1)
    mask = (rd < 15) & (rd > 0)
    errors = np.abs(rd - od)[mask]
    lddt = float(np.mean([(errors < t).mean() for t in (0.5, 1, 2, 4)]))
    return {"atoms": len(atoms), "residues": len(pred), "matched_reference_residues": len(pairs),
            "reference_residues": len(truth), "ca_lddt": lddt,
            "metric": "sequence-aligned CA-lDDT, 15A cutoff, 0.5/1/2/4A thresholds; not all-atom OpenStructure lDDT"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--cases", default="T1031,T1038,T1096")
    parser.add_argument("--samples", type=int, default=1)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    waiting = time.monotonic()
    while True:
        try:
            with urllib.request.urlopen(args.url + "/v1/health/ready", timeout=3) as response:
                readiness = json.load(response)
            (args.out / "readiness.json").write_text(json.dumps(readiness, indent=2))
            break
        except (urllib.error.URLError, TimeoutError):
            if time.monotonic() - waiting > 1800:
                raise
            time.sleep(5)
    cases = args.cases.split(",")
    schedule = [(cases[0], "smoke")]
    schedule += [(name, f"repeat-{repeat}") for repeat in range(args.repetitions)
                 for name in cases]
    schedule += [(name, "mixed-serial") for name in reversed(cases)]
    for index, (name, cohort) in enumerate(schedule):
        request_bytes = (args.fixtures / f"{name}.json").read_bytes()
        request_data = json.loads(request_bytes)
        if args.samples != 1:
            request_data["diffusion_samples"] = args.samples
            request_bytes = json.dumps(request_data, sort_keys=True).encode()
        record = {"variant": args.variant, "attempt": index, "fixture": name, "cohort": cohort,
                  "input_sha256": hashlib.sha256(request_bytes).hexdigest(), "started_unix": time.time()}
        started = time.monotonic()
        try:
            req = urllib.request.Request(args.url + "/biology/mit/boltz2/predict", data=request_bytes,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1200) as response:
                raw = response.read()
                record["http_status"] = response.status
            record["http_wall_seconds"] = time.monotonic() - started
            result = json.loads(raw)
            (args.out / f"{index:03d}-{name}.response.json").write_bytes(raw)
            assert len(result["structures"]) == request_data["diffusion_samples"]
            scores = result["confidence_scores"] + result["ptm_scores"]
            assert all(math.isfinite(x) and 0 <= x <= 1 for x in scores)
            record["quality"] = []
            for sample, structure in enumerate(result["structures"]):
                cif = structure["structure"]
                (args.out / f"{index:03d}-{name}-{sample}.cif").write_text(cif)
                reference = args.fixtures / f"{name}.pdb"
                if not reference.exists():
                    reference = args.fixtures / f"{name}.cif"
                record["quality"].append(quality(cif, str(reference),
                    sum(len(p["sequence"]) for p in request_data["polymers"])))
            record["confidence_scores"] = result["confidence_scores"]
            record["ptm_scores"] = result["ptm_scores"]
            record["status"] = "valid"
        except Exception as exc:
            record.update(status="failed", error=repr(exc), traceback=traceback.format_exc())
            if isinstance(exc, urllib.error.HTTPError):
                record["http_status"] = exc.code
                record["response_error"] = exc.read().decode()
        record["validated_wall_seconds"] = time.monotonic() - started
        with (args.out / "attempts.jsonl").open("a") as file:
            file.write(json.dumps(record) + "\n")
        print(json.dumps(record), flush=True)
        if cohort == "smoke" and record["status"] == "failed":
            break


if __name__ == "__main__":
    main()
