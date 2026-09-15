#!/usr/bin/env python3
"""Join lane evidence without converting unsupported/unmeasured results to zeros."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "boltz2", "openfold2", "openfold3", "diffdock", "evo2-40b", "genmol",
    "molmim", "msa-search-pdb70", "proteinmpnn", "rfdiffusion",
    "proteina-complexa", "protenix-v2",
}
RELATED = {"openfold3-openbind", "boltzgen", "mosaic", "bindcraft", "esmfold2",
           "esmfold2-fast", "alphafold3"}
REQUIRED = {
    "model_id", "bir_fit", "status", "baseline", "comparisons", "quality",
    "features", "snapshot", "recommendation", "limitations", "evidence",
}
SNAPSHOT_SOURCES = (
    "snapshot/result.json",
    "snapshot/protenix/result.json",
    "snapshot/openfold3/result.json",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    models, sources, missing_lanes = {}, [], []
    for lane in ("boltz2", "openfold", "coverage", "protenix"):
        source = ROOT / lane / "result.json"
        if not source.is_file():
            missing_lanes.append(lane)
            continue
        document = json.loads(source.read_text())
        if not isinstance(document.get("models"), list):
            raise ValueError(f"{lane}: models must be an array")
        for model in document["models"]:
            missing = REQUIRED - model.keys()
            if missing:
                raise ValueError(f"{lane}: missing required fields {sorted(missing)}")
            model_id = model["model_id"]
            if model_id in models or model_id not in EXPECTED | RELATED:
                raise ValueError(f"duplicate or out-of-scope model: {model_id}")
            models[model_id] = {**model, "lane_evidence": str(source.relative_to(ROOT))}
        sources.append(str(source.relative_to(ROOT)))
    missing_models = sorted(EXPECTED - models.keys())
    if missing_models and not args.allow_incomplete:
        raise ValueError(f"model coverage incomplete: {missing_models}")
    snapshot_studies, missing_snapshots = [], []
    for name in SNAPSHOT_SOURCES:
        path = ROOT / name
        if not path.is_file():
            missing_snapshots.append(name)
            continue
        snapshot_studies.append({
            "source": name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "result": json.loads(path.read_text()),
        })
        sources.append(name)
    review_path = ROOT / "report/review.json"
    review = json.loads(review_path.read_text()) if review_path.is_file() else {}
    review_current = not missing_models and not missing_lanes and not missing_snapshots and bool(review.get("passed")) and all(
        (ROOT / name).is_file()
        and hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
        for name, digest in review.get("source_sha256", {}).items()
    ) and set(sources).issubset(review.get("source_sha256", {}))
    result = {
        "schema": "fs2.bioir-comparison/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope_complete": not missing_models,
        "acceptance_review_complete": review_current,
        "production_promoted": False,
        "missing_models": missing_models,
        "missing_lanes": missing_lanes,
        "sources": sources,
        "missing_snapshot_studies": missing_snapshots,
        "snapshot_studies": snapshot_studies,
        "models": [models[key] for key in sorted(models) if key in EXPECTED],
        "related_models": [models[key] for key in sorted(models) if key in RELATED],
        "interpretation": [
            "Coverage completeness is not scientific equivalence or deployment approval.",
            "Only matched GPU/checkpoint/parameters are valid speedup pairs.",
            "Forward, request-window and allocated-GPU costs are different denominators.",
            "Unmeasured and unsupported improvements remain null or absent.",
        ],
    }
    (ROOT / "report/comparison.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"required_models": len(models.keys() & EXPECTED),
                      "related_models": len(models.keys() & RELATED),
                      "missing": missing_models, "sources": sources,
                      "review_current": review_current}))


if __name__ == "__main__":
    main()
