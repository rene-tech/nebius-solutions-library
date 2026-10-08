"""Run pinned, published CToxPred2 evaluation molecules; not a new clinical study.

Retains source hashes, all predictions and labels. ROC-AUC is descriptive on
upstream's evaluation sets, not evidence of new external validation. The runtime
is never fitted, and no customer input is used. Run inside either CPU image.
"""

import argparse
import csv
import hashlib
import io
import json
import time
from pathlib import Path
from urllib.request import urlopen

from qualify_runtime import validate_prediction

SOURCE = "https://raw.githubusercontent.com/issararab/CToxPred2/2a31aa119e27b6b69a5588d18a01f2a27fef4524/data"
SETS = {
    "hERG": "hERG/eval_set_herg_60.csv",
    "Cav1.2": "Cav1.2/eval_set_cav_60.csv",
    "Nav1.5": "Nav1.5/eval_set_nav_60.csv",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["admet-ai", "ctoxpred2"], required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.model == "admet-ai":
        from toxicology.admet import ADMETRuntime

        runtime = ADMETRuntime()
    else:
        from toxicology.ctox import CToxRuntime

        runtime = CToxRuntime()
    datasets = []
    for endpoint, path in SETS.items():
        url = SOURCE + "/" + path
        raw = urlopen(url, timeout=30).read()
        rows = list(csv.DictReader(io.StringIO(raw.decode())))
        smiles = [row["SMILES"] for row in rows]
        started = time.perf_counter()
        results = []
        for offset in range(0, len(smiles), 32):
            results.extend(runtime.predict(smiles[offset : offset + 32]))
        elapsed = time.perf_counter() - started
        validate_prediction(args.model, results, len(rows))
        record = {
            "endpoint": endpoint,
            "source_url": url,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "molecules": len(rows),
            "unique_smiles": len(set(smiles)),
            "seconds": elapsed,
            "molecules_per_second": len(rows) / elapsed,
            "predictions": results,
            "inputs_and_labels": rows,
            "passed": True,
        }
        if args.model == "ctoxpred2":
            from sklearn.metrics import (
                roc_auc_score,
                balanced_accuracy_score,
                brier_score_loss,
            )

            y = [int(float(row["pIC50"]) >= 5) for row in rows]
            probabilities = [
                row["predictions"][endpoint]["positive_class_score"] for row in results
            ]
            predicted = [
                row["predictions"][endpoint]["predicted_class"] for row in results
            ]
            record["descriptive_metrics"] = {
                "roc_auc": roc_auc_score(y, probabilities),
                "balanced_accuracy": balanced_accuracy_score(y, predicted),
                "brier": brier_score_loss(y, probabilities),
                "positive_labels": sum(y),
                "positive_class": "blocker IC50 <= 10 micromolar",
            }
        datasets.append(record)
    receipt = {
        "schema": "fs2-toxicology-published-set-evaluation/v1",
        "model_id": args.model,
        "passed": True,
        "clinical_validation": False,
        "external_validation_claimed": False,
        "datasets": datasets,
    }
    encoded = json.dumps(receipt, allow_nan=False)
    if args.output:
        args.output.write_text(encoded + "\n")
        print(
            json.dumps(
                {
                    "model": args.model,
                    "passed": True,
                    "receipt": str(args.output),
                    "datasets": [
                        {
                            k: v
                            for k, v in d.items()
                            if k not in {"predictions", "inputs_and_labels"}
                        }
                        for d in datasets
                    ],
                }
            ),
            flush=True,
        )
    else:
        print("FS2_RECEIPT=" + encoded, flush=True)


if __name__ == "__main__":
    main()
