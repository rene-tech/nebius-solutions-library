#!/usr/bin/env python3
"""Project validated benchmark receipts into the controller evidence envelope."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from aggregate_fast_start_benchmark import (
    FastStartEvidenceError,
    _timestamp,
    load_json,
    validate_receipt,
)

SUPPORTED_CACHE_TIERS = {"Disabled", "ObjectStore", "SharedFilesystem", "NodeLocal"}


def project_receipt(receipt: dict[str, Any], *, valid_for_days: int) -> tuple[str, dict[str, Any]]:
    """Return one backend ``FastStartEvidence`` document from a valid receipt."""

    validate_receipt(receipt)
    compatibility = receipt["compatibility_tuple"]
    cache_tier = compatibility["cache_tier"]
    if cache_tier not in SUPPORTED_CACHE_TIERS:
        raise FastStartEvidenceError(f"cache tier {cache_tier!r} has no ModelDeployment CacheTier representation")
    generated_at = _timestamp(receipt["generated_at"])
    samples = [
        {
            "observedAt": attempt["observed_at"],
            "modelStartSeconds": (
                attempt["durations_seconds"]["gpu_capacity_available_to_ready"] if attempt["status"] == "PASS" else None
            ),
            "capacityWaitSeconds": attempt["durations_seconds"]["capacity_wait"],
            "endToEndSeconds": attempt["durations_seconds"]["activation_to_ready"],
        }
        for attempt in receipt["attempts"]
    ]
    evidence = {
        "receiptDigest": f"sha256:{receipt['receipt_digest']}",
        "mechanism": compatibility["mechanism"],
        "mechanismConfigDigest": compatibility.get("mechanism_config_digest"),
        "compatibilityTupleDigest": f"sha256:{receipt['compatibility_tuple_digest']}",
        "compatibilityTupleComplete": receipt["qualification"]["compatibility_tuple_complete"],
        "measurementBasis": "CapacityAvailableToSemanticReady",
        "acceleratorClass": compatibility["accelerator_class"],
        "poolRef": compatibility["pool_id"],
        "acceleratorsPerReplica": compatibility["gpu_count"],
        "artifactManifestDigest": compatibility["artifact_manifest_digest"],
        "runtimeImage": compatibility["runtime_image_ref"],
        "templateDigest": compatibility["runtime_template_digest"],
        "cacheTier": cache_tier,
        "snapshotDigest": compatibility["snapshot_digest"],
        "samples": samples,
        "validUntil": (generated_at + timedelta(days=valid_for_days)).isoformat().replace("+00:00", "Z"),
    }
    return compatibility["model_id"], evidence


def project_receipts(receipts: Sequence[dict[str, Any]], *, valid_for_days: int) -> dict[str, list[dict[str, Any]]]:
    if not 1 <= valid_for_days <= 365:
        raise FastStartEvidenceError("valid-for-days must be between 1 and 365")
    projected: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    for receipt in receipts:
        model_id, evidence = project_receipt(receipt, valid_for_days=valid_for_days)
        digest = evidence["receiptDigest"]
        if digest in seen:
            raise FastStartEvidenceError(f"duplicate receipt: {digest}")
        seen.add(digest)
        projected.setdefault(model_id, []).append(evidence)
    return {model_id: projected[model_id] for model_id in sorted(projected)}


def _write_new(path: Path, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    except OSError as error:
        raise FastStartEvidenceError(f"output already exists or is unavailable: {path}") from error
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--valid-for-days", type=int, default=30)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        values = [load_json(path) for path in arguments.receipt]
        if any(not isinstance(value, dict) for value in values):
            raise FastStartEvidenceError("every receipt must be a JSON object")
        projected = project_receipts(values, valid_for_days=arguments.valid_for_days)
        _write_new(arguments.output, projected)
        print(json.dumps({"models": sorted(projected), "receipts": len(values)}, sort_keys=True))
        return 0
    except FastStartEvidenceError as error:
        print(f"fast-start evidence projection failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
