"""Deterministic collectors and semantic validators for structure adapters."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

from .common import (
    ARTIFACT_MANIFEST_SCHEMA,
    ArtifactPointer,
    CollectedOutput,
    LoadedArtifact,
    ScientificAdapterError,
    collect_output_files,
    structure_atom_count,
)

if TYPE_CHECKING:
    from . import CollectedStageOutput

CONFIDENCE_SCHEMA = "fs2.nebius.ai/structure-confidence/v1"
_METRIC_BOUNDS = {
    "plddt": (0.0, 100.0),
    "plddt_mean": (0.0, 100.0),
    "avg_plddt": (0.0, 100.0),
    "gpde": (0.0, 64.0),
    "ptm": (0.0, 1.0),
    "iptm": (0.0, 1.0),
    "fraction_disordered": (0.0, 1.0),
    "disorder": (0.0, 1.0),
    "ranking_score": (-100.0, 2.0),
    "sample_ranking_score": (-100.0, 2.0),
}
_STRUCTURE_TYPES = {
    ".pdb": ("chemical/x-pdb", "protein-structure-pdb/v1"),
    ".cif": ("chemical/x-mmcif", "protein-structure-mmcif/v1"),
    ".mmcif": ("chemical/x-mmcif", "protein-structure-mmcif/v1"),
}


def _contained_file(root: Path, relative: str, *, label: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ScientificAdapterError(f"{label} is not a safe relative path")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = (resolved_root / path).resolve(strict=True)
    except OSError as error:
        raise ScientificAdapterError(f"{label} is not atomically available") from error
    if resolved_root not in resolved.parents or not resolved.is_file() or (resolved_root / path).is_symlink():
        raise ScientificAdapterError(f"{label} is not a contained regular file")
    return resolved


def collect_handoff(
    workspace: Path,
    *,
    filename: str,
    name: str,
    semantic_type: str,
    media_type: str,
    maximum_bytes: int,
    compression: str | None = None,
) -> CollectedOutput:
    """Collect one bounded handoff without relying on mutable directory order."""

    output = _contained_file(workspace, filename, label="stage handoff")
    content = output.read_bytes()
    if not 1 <= len(content) <= maximum_bytes:
        raise ScientificAdapterError("stage handoff size is outside the adapter bound")
    digest = hashlib.sha256(content).hexdigest()
    artifact_id = f"result.{hashlib.sha256(name.encode()).hexdigest()[:16]}.{digest[:32]}"
    pointer = ArtifactPointer(
        artifact_id=artifact_id,
        sha256=digest,
        size_bytes=len(content),
        media_type=media_type,
        compression=compression,
    )
    return CollectedOutput(
        manifest={
            "schema": ARTIFACT_MANIFEST_SCHEMA,
            "manifest_id": f"handoff.{digest[:32]}",
            "entries": [{"name": name, "semantic_type": semantic_type, "artifact": pointer.to_dict()}],
        },
        blobs={artifact_id: content},
    )


def collect_handoff_stage(
    workspace: Path,
    *,
    filename: str,
    name: str,
    semantic_type: str,
    media_type: str,
    maximum_bytes: int,
    validator_id: str,
    compression: str | None = None,
) -> CollectedStageOutput:
    """Return one file-backed handoff for the production companion."""

    from . import CollectedArtifactFile, CollectedStageOutput, CollectionPendingError

    if not (workspace / filename).exists():
        raise CollectionPendingError("stage handoff is not atomically available")
    output = _contained_file(workspace, filename, label="stage handoff")
    size = output.stat().st_size
    if not 1 <= size <= maximum_bytes:
        raise ScientificAdapterError("stage handoff size is outside the adapter bound")
    return CollectedStageOutput(
        artifacts=(
            CollectedArtifactFile(
                name=name,
                semantic_type=semantic_type,
                path=output,
                media_type=media_type,
                compression=compression,
            ),
        ),
        validation={
            "validator_id": validator_id,
            "status": "passed",
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "size_bytes": size,
        },
    )


def _validated_confidence_entries(
    workspace: Path,
    *,
    expected_runtime_id: str,
    expected_model_revision: str,
    expected_seeds: tuple[int, ...],
    expected_samples_per_seed: int,
) -> tuple[tuple[tuple[str, str, Path, bool], ...], dict[str, object]]:
    root = workspace.resolve(strict=True)
    output_root = (root / "outputs").resolve()
    if root not in output_root.parents:
        raise ScientificAdapterError("outputs directory escapes the attempt workspace")
    confidence_path = _contained_file(root, "outputs/confidence.json", label="confidence envelope")
    if confidence_path.stat().st_size > 16 * 1024 * 1024:
        raise ScientificAdapterError("confidence envelope exceeds the byte bound")
    try:
        envelope = json.loads(confidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ScientificAdapterError("confidence envelope is not valid UTF-8 JSON") from error
    expected_fields = {
        "schema",
        "runtime_id",
        "model_revision",
        "seeds",
        "samples_per_seed",
        "results",
    }
    results = envelope.get("results") if isinstance(envelope, dict) else None
    if (
        not isinstance(envelope, dict)
        or set(envelope) != expected_fields
        or envelope["schema"] != CONFIDENCE_SCHEMA
        or envelope["runtime_id"] != expected_runtime_id
        or envelope["seeds"] != list(expected_seeds)
        or envelope["samples_per_seed"] != expected_samples_per_seed
        or envelope["model_revision"] != expected_model_revision
        or not isinstance(results, list)
        or len(results) != len(expected_seeds) * expected_samples_per_seed
    ):
        raise ScientificAdapterError("confidence envelope identity or cardinality is invalid")

    entries: list[tuple[str, str, Path, bool]] = []
    pairs: set[tuple[int, int]] = set()
    filenames: set[str] = set()
    summary_names: set[str] = set()
    total_atoms = 0
    for result in results:
        if not isinstance(result, dict) or set(result) != {
            "seed",
            "sample_index",
            "upstream_summary",
            "structure",
            "metrics",
        }:
            raise ScientificAdapterError("confidence result has an unexpected shape")
        seed, sample = result["seed"], result["sample_index"]
        structure, metrics = result["structure"], result["metrics"]
        if (
            isinstance(seed, bool)
            or not isinstance(seed, int)
            or seed not in expected_seeds
            or isinstance(sample, bool)
            or not isinstance(sample, int)
            or not 0 <= sample < expected_samples_per_seed
            or not isinstance(structure, dict)
            or set(structure) != {"filename", "sha256", "bytes"}
            or not isinstance(metrics, dict)
            or not 1 <= len(metrics) <= 16
            or not set(metrics) <= set(_METRIC_BOUNDS)
        ):
            raise ScientificAdapterError("confidence result is invalid or unbounded")
        pair = (seed, sample)
        filename = structure["filename"]
        if pair in pairs or not isinstance(filename, str) or filename in filenames:
            raise ScientificAdapterError("confidence result binding is not one-to-one")
        path = _contained_file(output_root, filename, label="confidence structure")
        type_pair = _STRUCTURE_TYPES.get(path.suffix.lower())
        content = path.read_bytes()
        if (
            type_pair is None
            or structure["bytes"] != len(content)
            or structure["sha256"] != hashlib.sha256(content).hexdigest()
        ):
            raise ScientificAdapterError("confidence structure type, digest, or size does not match")
        media_type, semantic_type = type_pair
        loaded = LoadedArtifact(
            name=f"prediction.{seed}.{sample}",
            semantic_type=semantic_type,
            pointer=ArtifactPointer(
                artifact_id=f"structure.{seed}.{sample}",
                sha256=str(structure["sha256"]),
                size_bytes=len(content),
                media_type=media_type,
            ),
            content=content,
        )
        atom_count = structure_atom_count(loaded, require_two_chains=False)
        if atom_count < 10:
            raise ScientificAdapterError("structure must contain at least ten ATOM/HETATM records")
        total_atoms += atom_count
        for metric_name, value in metrics.items():
            minimum, maximum = _METRIC_BOUNDS[metric_name]
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or not minimum <= float(value) <= maximum
            ):
                raise ScientificAdapterError(f"confidence metric {metric_name} is invalid")
        entries.append((f"prediction.{seed}.{sample}", semantic_type, path, False))

        summary = result["upstream_summary"]
        if summary is not None:
            if not isinstance(summary, str) or summary in summary_names:
                raise ScientificAdapterError("upstream summary binding is invalid")
            summary_path = _contained_file(output_root, summary, label="upstream summary")
            if summary_path.suffix.lower() != ".json" or summary_path.stat().st_size > 64 * 1024 * 1024:
                raise ScientificAdapterError("upstream summary must be bounded JSON")
            try:
                json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ScientificAdapterError("upstream summary is not valid UTF-8 JSON") from error
            entries.append(
                (
                    f"upstream-summary.{seed}.{sample}",
                    "structure-upstream-summary-json/v1",
                    summary_path,
                    False,
                )
            )
            summary_names.add(summary)

        pairs.add(pair)
        filenames.add(filename)

    expected_pairs = {(seed, sample) for seed in expected_seeds for sample in range(expected_samples_per_seed)}
    if pairs != expected_pairs:
        raise ScientificAdapterError("confidence results do not cover the requested seed/sample product")
    entries.append(("confidence", "structure-confidence-json/v1", confidence_path, False))
    return (
        tuple(entries),
        {
            "validator_id": expected_runtime_id,
            "status": "passed",
            "model_revision": expected_model_revision,
            "structure_count": len(results),
            "atom_count": total_atoms,
        },
    )


def validate_confidence_envelope(
    workspace: Path,
    *,
    validator_id: str,
    expected_runtime_id: str,
    expected_model_revision: str,
    expected_seeds: tuple[int, ...],
    expected_samples_per_seed: int,
) -> dict[str, object]:
    """Validate exact backend identity, cardinality, metrics, and structure bytes."""

    _entries, validation = _validated_confidence_entries(
        workspace,
        expected_runtime_id=expected_runtime_id,
        expected_model_revision=expected_model_revision,
        expected_seeds=expected_seeds,
        expected_samples_per_seed=expected_samples_per_seed,
    )
    validation["validator_id"] = validator_id
    return validation


def collect_confidence_envelope(
    workspace: Path,
    *,
    validator_id: str,
    expected_runtime_id: str,
    expected_model_revision: str,
    expected_seeds: tuple[int, ...],
    expected_samples_per_seed: int,
    maximum_total_bytes: int,
) -> CollectedOutput:
    """Validate and collect one canonical confidence envelope and its closure."""

    entries, validation = _validated_confidence_entries(
        workspace,
        expected_runtime_id=expected_runtime_id,
        expected_model_revision=expected_model_revision,
        expected_seeds=expected_seeds,
        expected_samples_per_seed=expected_samples_per_seed,
    )
    if validation["status"] != "passed":
        raise ScientificAdapterError("secondary structure validation did not pass")
    return collect_output_files(
        workspace,
        entries,
        manifest_id=(
            f"{expected_runtime_id}.results.{hashlib.sha256(expected_model_revision.encode('utf-8')).hexdigest()[:24]}"
        ),
        maximum_total_bytes=maximum_total_bytes,
    )


def collect_confidence_stage(
    workspace: Path,
    *,
    validator_id: str,
    expected_runtime_id: str,
    expected_model_revision: str,
    expected_seeds: tuple[int, ...],
    expected_samples_per_seed: int,
    maximum_total_bytes: int,
) -> CollectedStageOutput:
    """Validate a confidence closure and expose its existing files to the companion."""

    from . import CollectedArtifactFile, CollectedStageOutput, CollectionPendingError

    if not (workspace / "outputs/confidence.json").exists():
        raise CollectionPendingError("confidence envelope is not atomically available")
    entries, validation = _validated_confidence_entries(
        workspace,
        expected_runtime_id=expected_runtime_id,
        expected_model_revision=expected_model_revision,
        expected_seeds=expected_seeds,
        expected_samples_per_seed=expected_samples_per_seed,
    )
    artifacts: list[CollectedArtifactFile] = []
    total = 0
    for name, semantic_type, path, sanitize_csv in entries:
        if sanitize_csv:
            raise ScientificAdapterError("secondary confidence output unexpectedly requires rewriting")
        total += path.stat().st_size
        if total > maximum_total_bytes:
            raise ScientificAdapterError("collected outputs exceed the byte bound")
        suffix = path.suffix.lower()
        media_type = (
            "application/json"
            if semantic_type.endswith("json/v1") or suffix == ".json"
            else "chemical/x-mmcif"
            if suffix in {".cif", ".mmcif"}
            else "chemical/x-pdb"
        )
        artifacts.append(
            CollectedArtifactFile(
                name=name,
                semantic_type=semantic_type,
                path=path,
                media_type=media_type,
            )
        )
    validation["validator_id"] = validator_id
    return CollectedStageOutput(artifacts=tuple(artifacts), validation=validation)
