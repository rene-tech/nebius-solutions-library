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
METRIC_BOUNDS = {
    "plddt": (0.0, 100.0),
    "plddt_mean": (0.0, 1.0),
    "avg_plddt": (0.0, 100.0),
    "gpde": (0.0, 64.0),
    "ptm": (0.0, 1.0),
    "iptm": (0.0, 1.0),
    "fraction_disordered": (0.0, 1.0),
    "disorder": (0.0, 1.0),
    "ranking_score": (-100.0, 2.0),
    "sample_ranking_score": (-100.0, 2.0),
}


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
        or any(
            not isinstance(seed, int)
            or isinstance(seed, bool)
            or seed < 0
            or seed > 2**32 - 1
            for seed in seeds
        )
        or len(set(seeds)) != len(seeds)
        or not 1 <= samples_per_seed <= MAX_SAMPLES_PER_SEED
    ):
        raise SystemExit("confidence envelope seed/sample bounds are invalid")
    if len(results) != len(seeds) * samples_per_seed:
        raise SystemExit("confidence result count must equal the exact seed/sample product")

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
            or not set(metrics).issubset(METRIC_BOUNDS)
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not METRIC_BOUNDS[name][0]
                <= float(value)
                <= METRIC_BOUNDS[name][1]
                for name, value in metrics.items()
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
    seed_order = {seed: index for index, seed in enumerate(seeds)}
    normalized.sort(
        key=lambda item: (
            seed_order[int(item["seed"])],
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


def validate_confidence_envelope(
    output_dir: Path,
    envelope: object,
    *,
    expected_runtime_id: str | None = None,
    expected_seeds: list[int] | None = None,
    expected_samples_per_seed: int | None = None,
) -> dict[str, object]:
    """Validate the canonical envelope and every referenced local artifact."""
    if not isinstance(envelope, dict) or set(envelope) != {
        "schema",
        "runtime_id",
        "model_revision",
        "seeds",
        "samples_per_seed",
        "results",
    }:
        raise SystemExit("confidence envelope has an unexpected shape")
    seeds = envelope.get("seeds")
    samples = envelope.get("samples_per_seed")
    results = envelope.get("results")
    runtime_id = envelope.get("runtime_id")
    if (
        envelope.get("schema") != "fs2.nebius.ai/structure-confidence/v1"
        or not isinstance(runtime_id, str)
        or not runtime_id
        or not isinstance(envelope.get("model_revision"), str)
        or not envelope["model_revision"]
        or not isinstance(seeds, list)
        or not 1 <= len(seeds) <= MAX_SEEDS
        or any(
            not isinstance(seed, int)
            or isinstance(seed, bool)
            or not 0 <= seed <= 2**32 - 1
            for seed in seeds
        )
        or len(set(seeds)) != len(seeds)
        or not isinstance(samples, int)
        or isinstance(samples, bool)
        or not 1 <= samples <= MAX_SAMPLES_PER_SEED
        or not isinstance(results, list)
        or len(results) != len(seeds) * samples
    ):
        raise SystemExit("confidence envelope seed/sample cardinality is invalid")
    if expected_runtime_id is not None and runtime_id != expected_runtime_id:
        raise SystemExit("confidence envelope runtime identity does not match")
    if expected_seeds is not None and seeds != expected_seeds:
        raise SystemExit("confidence envelope ordered seeds do not match")
    if expected_samples_per_seed is not None and samples != expected_samples_per_seed:
        raise SystemExit("confidence envelope sample count does not match")

    pairs: set[tuple[int, int]] = set()
    structures: set[str] = set()
    summaries: set[str] = set()
    for result in results:
        if not isinstance(result, dict) or set(result) != {
            "seed",
            "sample_index",
            "upstream_summary",
            "structure",
            "metrics",
        }:
            raise SystemExit("confidence result has an unexpected shape")
        seed = result["seed"]
        sample_index = result["sample_index"]
        structure = result["structure"]
        summary = result["upstream_summary"]
        metrics = result["metrics"]
        if (
            not isinstance(seed, int)
            or isinstance(seed, bool)
            or seed not in seeds
            or not isinstance(sample_index, int)
            or isinstance(sample_index, bool)
            or not 0 <= sample_index < samples
            or not isinstance(structure, dict)
            or set(structure) != {"filename", "sha256", "bytes"}
            or not isinstance(metrics, dict)
            or not 1 <= len(metrics) <= 16
            or not set(metrics).issubset(METRIC_BOUNDS)
        ):
            raise SystemExit("confidence result is invalid or unbounded")
        pair = (seed, sample_index)
        filename = structure.get("filename")
        if (
            pair in pairs
            or not isinstance(filename, str)
            or Path(filename).is_absolute()
            or ".." in Path(filename).parts
            or filename in structures
        ):
            raise SystemExit("confidence result structure binding is not one-to-one")
        path = output_dir / filename
        if (
            not path.is_file()
            or path.suffix.lower() not in STRUCTURE_SUFFIXES
            or structure.get("bytes") != path.stat().st_size
            or structure.get("sha256") != sha256_file(path)
        ):
            raise SystemExit("confidence result does not bind its structure bytes")
        if summary is not None:
            if (
                not isinstance(summary, str)
                or Path(summary).is_absolute()
                or ".." in Path(summary).parts
                or summary in summaries
                or not (output_dir / summary).is_file()
            ):
                raise SystemExit("confidence result summary binding is invalid")
            summaries.add(summary)
        for name, value in metrics.items():
            minimum, maximum = METRIC_BOUNDS[name]
            finite_metric(name, value, minimum, maximum)
        pairs.add(pair)
        structures.add(filename)
    expected_pairs = {(seed, sample) for seed in seeds for sample in range(samples)}
    if pairs != expected_pairs:
        raise SystemExit("confidence results do not cover the exact seed/sample product")
    return envelope
