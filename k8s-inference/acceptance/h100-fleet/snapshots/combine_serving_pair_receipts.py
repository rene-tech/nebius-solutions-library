#!/usr/bin/env python3
"""Combine completed matched-pair sessions without changing measured fields."""

import argparse
import hashlib
import json
import os
from pathlib import Path


def combine(paths):
    receipts = [json.loads(path.read_bytes()) for path in paths]
    if not receipts or any(receipt["status"] != "passed" for receipt in receipts):
        raise ValueError("every source receipt must be complete and passed")
    models = {receipt["model"] for receipt in receipts}
    caches = {receipt["cache"] for receipt in receipts}
    if len(models) != 1 or len(caches) != 1:
        raise ValueError("source receipts must measure one identical model/cache condition")
    pairs = []
    for receipt_index, receipt in enumerate(receipts, 1):
        grouped = {}
        for row in receipt["runs"]:
            grouped.setdefault(row["repetition"], {})[row["mode"]] = row
        for source_repetition, pair in sorted(grouped.items()):
            if set(pair) != {"normal", "restore"}:
                raise ValueError("every source repetition must contain a matched pair")
            pairs.append((receipt_index, source_repetition, pair))
    if len(pairs) != 3:
        raise ValueError("exactly three completed source pairs are required")
    runs = []
    for repetition, (receipt_index, source_repetition, pair) in enumerate(pairs, 1):
        for mode in ("normal", "restore"):
            row = dict(pair[mode])
            row["repetition"] = repetition
            row["source_receipt_index"] = receipt_index
            row["source_repetition"] = source_repetition
            runs.append(row)
    return {
        "model": next(iter(models)),
        "cache": next(iter(caches)),
        "status": "passed",
        "runs": runs,
        "source_receipts": [
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in paths
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    result = combine(args.receipt)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
