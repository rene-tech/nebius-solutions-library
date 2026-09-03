"""Shared bounded output handling for secondary structure adapters."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from ..models import StageInvocation
from . import CollectedArtifactFile, CollectedStageOutput, CollectionPendingError
from .common import ArtifactPointer, LoadedArtifact, ScientificAdapterError, structure_atom_count

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


def collect_handoff(
    invocation: StageInvocation,
    workspace: Path,
    *,
    filename: str,
    semantic_type: str,
    media_type: str,
    compression: str | None = None,
) -> CollectedStageOutput:
    """Collect one deterministic stage handoff selected by the invocation."""

    output = (workspace / filename).resolve()
    root = workspace.resolve()
    if root not in output.parents or not output.is_file() or output.is_symlink():
        raise CollectionPendingError(f"stage handoff is not atomically available: {filename}")
    if invocation.handoff_name is None:
        raise ScientificAdapterError("consumed stage output requires a handoff name")
    return CollectedStageOutput(
        artifacts=(
            CollectedArtifactFile(
                name=invocation.handoff_name,
                semantic_type=semantic_type,
                path=output,
                media_type=media_type,
                compression=compression,
            ),
        ),
        validation={
            "validator_id": invocation.validator_id,
            "status": "passed",
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        },
    )


def collect_confidence_envelope(
    invocation: StageInvocation,
    workspace: Path,
    *,
    expected_runtime_id: str,
    expected_model_revision: str,
    expected_seeds: tuple[int, ...],
    expected_samples_per_seed: int,
) -> CollectedStageOutput:
    """Collect exactly one canonical confidence envelope and its bound structures."""

    root = workspace.resolve()
    output_root = (root / "outputs").resolve()
    confidence_path = output_root / "confidence.json"
    if not confidence_path.is_file() or confidence_path.is_symlink():
        raise CollectionPendingError("outputs/confidence.json is not atomically available")
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
    if not isinstance(envelope, dict) or set(envelope) != expected_fields:
        raise ScientificAdapterError("confidence envelope has an unexpected shape")
    results = envelope["results"]
    if (
        envelope["schema"] != CONFIDENCE_SCHEMA
        or envelope["runtime_id"] != expected_runtime_id
        or envelope["seeds"] != list(expected_seeds)
        or envelope["samples_per_seed"] != expected_samples_per_seed
        or envelope["model_revision"] != expected_model_revision
        or not isinstance(results, list)
        or len(results) != len(expected_seeds) * expected_samples_per_seed
    ):
        raise ScientificAdapterError("confidence envelope identity or cardinality is invalid")
    artifacts: list[CollectedArtifactFile] = []
    pairs: set[tuple[int, int]] = set()
    filenames: set[str] = set()
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
        if (
            pair in pairs
            or not isinstance(filename, str)
            or Path(filename).is_absolute()
            or ".." in Path(filename).parts
            or filename in filenames
        ):
            raise ScientificAdapterError("confidence result binding is not one-to-one")
        path = (output_root / filename).resolve()
        if output_root not in path.parents or not path.is_file() or path.is_symlink():
            raise ScientificAdapterError("confidence structure is not a contained regular file")
        content = path.read_bytes()
        if structure["bytes"] != len(content) or structure["sha256"] != hashlib.sha256(content).hexdigest():
            raise ScientificAdapterError("confidence structure digest or size does not match")
        suffix = path.suffix.lower()
        is_pdb = suffix == ".pdb"
        media_type = "chemical/x-pdb" if is_pdb else "chemical/x-mmcif"
        semantic_type = "protein-structure-pdb/v1" if is_pdb else "protein-structure-mmcif/v1"
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
        for name, value in metrics.items():
            minimum, maximum = _METRIC_BOUNDS[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or not minimum <= float(value) <= maximum
            ):
                raise ScientificAdapterError(f"confidence metric {name} is invalid")
        artifacts.append(
            CollectedArtifactFile(
                name=f"prediction.{seed}.{sample}",
                semantic_type=semantic_type,
                path=path,
                media_type=media_type,
            )
        )
        pairs.add(pair)
        filenames.add(filename)
    if pairs != {(seed, sample) for seed in expected_seeds for sample in range(expected_samples_per_seed)}:
        raise ScientificAdapterError("confidence results do not cover the requested seed/sample product")
    artifacts.append(
        CollectedArtifactFile(
            name="confidence",
            semantic_type="structure-confidence-json/v1",
            path=confidence_path,
            media_type="application/json",
        )
    )
    return CollectedStageOutput(
        artifacts=tuple(artifacts),
        validation={
            "validator_id": invocation.validator_id,
            "status": "passed",
            "backend_id": expected_runtime_id,
            "structure_count": len(results),
            "atom_count": total_atoms,
        },
    )
