#!/usr/bin/env python3
"""Bounded, deterministic structure-result confidence envelopes."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Mapping


MAX_SEEDS = 16
MAX_SAMPLES_PER_SEED = 16
STRUCTURE_SUFFIXES = {".cif", ".mmcif", ".pdb"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_bounded_metrics(
    path: Path,
    bounds: Mapping[str, tuple[float, float]],
    *,
    required: set[str],
) -> dict[str, float | int]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"upstream confidence is invalid JSON: {path}: {exc}") from exc
    if not isinstance(document, dict) or not required.issubset(document):
        raise SystemExit(f"upstream confidence is missing required metrics: {path}")
    metrics: dict[str, float | int] = {}
    for name, (minimum, maximum) in bounds.items():
        if name not in document:
            continue
        value = document[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not minimum <= float(value) <= maximum
        ):
            raise SystemExit(
                f"upstream metric {name!r} must be a finite scalar in "
                f"[{minimum}, {maximum}]: {path}"
            )
        metrics[name] = value
    if not metrics:
        raise SystemExit(f"upstream confidence has no accepted bounded metrics: {path}")
    return metrics


def finite_metric(name: str, value: object, minimum: float, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise SystemExit(
            f"metric {name!r} must be a finite scalar in [{minimum}, {maximum}]"
        )
    return float(value)


def write_confidence_envelope(
    output_dir: Path,
    *,
    runtime_id: str,
    model_revision: str,
    seeds: list[int],
    samples_per_seed: int,
    results: list[dict[str, object]],
) -> dict[str, object]:
    if (
        not 1 <= len(seeds) <= MAX_SEEDS
        or len(set(seeds)) != len(seeds)
        or any(
            not isinstance(seed, int)
            or isinstance(seed, bool)
            or seed < 0
            or seed > 2**32 - 1
            for seed in seeds
        )
        or not 1 <= samples_per_seed <= MAX_SAMPLES_PER_SEED
    ):
        raise SystemExit("confidence envelope seed/sample bounds are invalid")
    if not results or len(results) > len(seeds) * samples_per_seed:
        raise SystemExit("confidence result count exceeds the bounded seed/sample product")

    normalized: list[dict[str, object]] = []
    seen_pairs: set[tuple[int, int]] = set()
    seen_structures: set[str] = set()
    seen_summaries: set[str] = set()
    for result in results:
        if set(result) != {"seed", "sample_index", "structure", "summary", "metrics"}:
            raise SystemExit("confidence result has an unexpected shape")
        seed = result["seed"]
        sample_index = result["sample_index"]
        structure = result["structure"]
        summary = result["summary"]
        metrics = result["metrics"]
        if (
            not isinstance(seed, int)
            or isinstance(seed, bool)
            or seed not in seeds
            or not isinstance(sample_index, int)
            or isinstance(sample_index, bool)
            or not 0 <= sample_index < samples_per_seed
            or not isinstance(structure, Path)
            or not structure.is_file()
            or structure.suffix.lower() not in STRUCTURE_SUFFIXES
            or structure.stat().st_size < 1
            or not isinstance(metrics, dict)
            or not metrics
            or len(metrics) > 16
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in metrics.values()
            )
        ):
            raise SystemExit("confidence result does not bind a valid bounded structure sample")
        try:
            structure_name = structure.relative_to(output_dir).as_posix()
        except ValueError as exc:
            raise SystemExit("confidence structure must be below output-dir") from exc
        summary_name: str | None = None
        if summary is not None:
            if not isinstance(summary, Path) or not summary.is_file():
                raise SystemExit("confidence result summary must be an existing file")
            try:
                summary_name = summary.relative_to(output_dir).as_posix()
            except ValueError as exc:
                raise SystemExit("confidence summary must be below output-dir") from exc
        pair = (seed, sample_index)
        if (
            pair in seen_pairs
            or structure_name in seen_structures
            or (summary_name is not None and summary_name in seen_summaries)
        ):
            raise SystemExit("confidence results are not one-to-one")
        seen_pairs.add(pair)
        seen_structures.add(structure_name)
        if summary_name is not None:
            seen_summaries.add(summary_name)
        normalized.append(
            {
                "seed": seed,
                "sample_index": sample_index,
                "upstream_summary": summary_name,
                "structure": {
                    "filename": structure_name,
                    "sha256": sha256_file(structure),
                    "bytes": structure.stat().st_size,
                },
                "metrics": dict(sorted(metrics.items())),
            }
        )
    normalized.sort(
        key=lambda item: (
            int(item["seed"]),
            int(item["sample_index"]),
            str(item["structure"]["filename"]),  # type: ignore[index]
        )
    )
    envelope: dict[str, object] = {
        "schema": "fs2.nebius.ai/structure-confidence/v1",
        "runtime_id": runtime_id,
        "model_revision": model_revision,
        "seeds": seeds,
        "samples_per_seed": samples_per_seed,
        "results": normalized,
    }
    encoded = json.dumps(
        envelope,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    (output_dir / "confidence.json").write_text(encoded + "\n", encoding="utf-8")
    return envelope
