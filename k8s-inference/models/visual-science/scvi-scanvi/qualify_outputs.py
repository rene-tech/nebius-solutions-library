"""Recheck downloaded single-cell output bytes and semantics, not accuracy."""

import argparse
import csv
import hashlib
import json
import math
from contextlib import ExitStack
from itertools import zip_longest
from pathlib import Path


def validate(result_path, data):
    result = json.loads(result_path.read_text())
    if result["status"] != "succeeded":
        raise ValueError("A failed worker result cannot qualify output")
    total = 0
    for item in result["files"]:
        path = (data / item["path"]).resolve()
        if not path.is_relative_to(data.resolve()):
            raise ValueError("Invalid output path")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != item["sha256"] or path.stat().st_size != item["size_bytes"]:
            raise ValueError("Output inventory digest or byte count mismatch")
        total += item["size_bytes"]
    with ExitStack() as stack:
        latent = csv.reader(
            stack.enter_context((data / "latent_embeddings.csv").open())
        )
        latent_header = next(latent)
        if len(latent_header) != result["latent_dimensions"] + 1:
            raise ValueError("Latent dimension mismatch")
        annotation = result["parameters"]["method"] == "scanvi"
        if annotation:
            labels = csv.reader(
                stack.enter_context((data / "predicted_labels.csv").open())
            )
            probabilities = csv.reader(
                stack.enter_context((data / "label_probabilities.csv").open())
            )
            next(labels)
            names = next(probabilities)[1:]
            rows = zip_longest(latent, labels, probabilities)
        else:
            rows = ((row, None, None) for row in latent)
        seen, maximum_error = set(), 0.0
        for embedding, label, probability in rows:
            if embedding is None or len(embedding) != len(latent_header):
                raise ValueError("Output row counts or latent width differ")
            if embedding[0] in seen or not all(
                math.isfinite(float(x)) for x in embedding[1:]
            ):
                raise ValueError("Duplicate cell or non-finite embedding")
            seen.add(embedding[0])
            if annotation:
                if (
                    label is None
                    or probability is None
                    or label[0] != embedding[0]
                    or probability[0] != embedding[0]
                ):
                    raise ValueError("Annotation rows are not aligned to embeddings")
                scores = [float(x) for x in probability[1:]]
                if len(scores) != len(names) or not all(
                    math.isfinite(x) and 0 <= x <= 1 for x in scores
                ):
                    raise ValueError("Invalid class probabilities")
                error = abs(math.fsum(scores) - 1)
                maximum_error = max(maximum_error, error)
                if (
                    error > 1e-4
                    or label[1]
                    != names[max(range(len(scores)), key=scores.__getitem__)]
                ):
                    raise ValueError(
                        "Probabilities are not normalized or label differs from argmax"
                    )
        if len(seen) != result["cells"]:
            raise ValueError("Exported cell count does not match worker result")
    return {
        "status": "passed",
        "operation_id": result["operation_id"],
        "cells": len(seen),
        "latent_dimensions": result["latent_dimensions"],
        "files_verified": len(result["files"]),
        "bytes_verified": total,
        "annotation_validated": annotation,
        "maximum_probability_sum_error": maximum_error if annotation else None,
        "biological_accuracy_or_convergence_claimed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = validate(args.result, args.data)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
