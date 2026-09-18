#!/usr/bin/env python3
"""Reopen isolated outputs, validate seed/hash contracts and measure references."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from prepare import CASES, ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--evaluators", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "models/cancer-immunotherapy/images/structure-secondary"))
    from result_contract import validate_confidence_envelope
    sys.path.insert(0, str(args.evaluators))
    from evaluators import evaluate
    manifest = json.loads((args.fixtures / "cases.json").read_text())
    results = []
    for pdb, seed in CASES:
        case_id = f"openfold3-openbind-{pdb}-heteromer-s{seed}"
        case = next(c for c in manifest["cases"] if c["case_id"] == case_id)
        output = args.directory / "outputs" / f"{pdb}-s{seed}" / "semantic"
        confidence = json.loads((output / "confidence.json").read_text())
        validated = validate_confidence_envelope(output, confidence, expected_runtime_id="openfold3",
                                                expected_seeds=[seed], expected_samples_per_seed=1)
        raw = args.fixtures / "references" / pdb / f"{case_id}-input.json"
        raw_sha = hashlib.sha256(raw.read_bytes()).hexdigest()
        if confidence.get("raw_input_sha256") != raw_sha:
            raise ValueError("Output does not bind the exact original source input")
        structures = [(output / r["structure"]["filename"]).read_text() for r in validated["results"]]
        evaluation = evaluate(case, {"structures": structures}, args.fixtures)
        if not evaluation["service_semantic_pass"]:
            raise ValueError("Independent exact-chain structure validation failed")
        results.append({"case_id": case_id, "seed": seed, "raw_input_sha256": raw_sha,
                        "confidence_sha256": hashlib.sha256((output / "confidence.json").read_bytes()).hexdigest(),
                        "result_count": len(structures), "evaluation": evaluation})
    receipt = {"schema": "openfold3-inline-isolated-validation/v1", "cases": results,
               "scope": "Exact H100 runtime/input/seed/artifact and chain integrity; reference accuracy is measured, not guaranteed; public profile replay remains separate"}
    (args.directory / "independent-validation.json").write_text(json.dumps(receipt, indent=2)+"\n")
    print(json.dumps({"cases": len(results), "exact_chain_integrity": True}))


if __name__ == "__main__":
    main()
