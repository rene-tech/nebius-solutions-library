"""Shared bounded output handling for secondary structure adapters."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .common import (
    ArtifactLoader,
    CollectedOutput,
    ScientificAdapterError,
    collect_output_files,
    finite_number,
    load_output_manifest,
    parse_json_artifact,
    strict_object,
    structure_atom_count,
)

CONFIDENCE_SCHEMA = "fs2.nebius.ai/structure-confidence/v1"
_RUNTIME_IDS = {"esmfold2", "esmfold2-fast", "protenix-v2", "alphafold3", "openfold3"}
_METRIC_BOUNDS = {
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


@dataclass(frozen=True, slots=True)
class _ConfidenceResult:
    structure: PurePosixPath
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _ConfidenceEnvelope:
    runtime_id: str
    model_revision: str
    seeds: tuple[int, ...]
    samples_per_seed: int
    results: tuple[_ConfidenceResult, ...]


def _safe_relative_filename(value: object, *, label: str, structure: bool) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "://" in value
        or any(character == "\x7f" or ord(character) < 32 for character in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ScientificAdapterError(f"{label} is not a safe relative POSIX filename")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ScientificAdapterError(f"{label} is not a safe relative POSIX filename")
    if structure and path.suffix.lower() not in {".cif", ".mmcif", ".pdb"}:
        raise ScientificAdapterError(f"{label} does not identify a supported structure file")
    return path


def _confidence_envelope(content: bytes, *, maximum_structures: int) -> _ConfidenceEnvelope:
    document = strict_object(
        parse_json_artifact(content, label="structure confidence envelope"),
        required=frozenset(
            {"schema", "runtime_id", "model_revision", "seeds", "samples_per_seed", "results"}
        ),
        label="structure confidence envelope",
    )
    if document["schema"] != CONFIDENCE_SCHEMA:
        raise ScientificAdapterError("structure confidence envelope has an unknown schema")
    runtime_id = document["runtime_id"]
    model_revision = document["model_revision"]
    raw_seeds = document["seeds"]
    samples_per_seed = document["samples_per_seed"]
    if not isinstance(runtime_id, str) or runtime_id not in _RUNTIME_IDS:
        raise ScientificAdapterError("structure confidence runtime identity is invalid")
    if not isinstance(model_revision, str) or not model_revision or len(model_revision) > 128:
        raise ScientificAdapterError("structure confidence model revision is invalid")
    if (
        not isinstance(raw_seeds, list)
        or not 1 <= len(raw_seeds) <= 16
        or any(
            isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1
            for seed in raw_seeds
        )
        or len(raw_seeds) != len(set(raw_seeds))
    ):
        raise ScientificAdapterError("structure confidence seeds are invalid")
    if (
        isinstance(samples_per_seed, bool)
        or not isinstance(samples_per_seed, int)
        or not 1 <= samples_per_seed <= 16
    ):
        raise ScientificAdapterError("structure confidence samples_per_seed is invalid")
    seeds = tuple(raw_seeds)
    raw_results = document["results"]
    expected_pairs = {(seed, sample) for seed in seeds for sample in range(samples_per_seed)}
    if (
        not isinstance(raw_results, list)
        or len(raw_results) != len(expected_pairs)
        or len(raw_results) > maximum_structures
    ):
        raise ScientificAdapterError("structure confidence result count does not match seeds and samples")
    results: list[_ConfidenceResult] = []
    paths: set[PurePosixPath] = set()
    summaries: set[PurePosixPath] = set()
    pairs: set[tuple[int, int]] = set()
    for index, raw in enumerate(raw_results):
        item = strict_object(
            raw,
            required=frozenset({"seed", "sample_index", "upstream_summary", "structure", "metrics"}),
            label=f"structure confidence results[{index}]",
        )
        seed, sample_index = item["seed"], item["sample_index"]
        if (
            isinstance(seed, bool)
            or not isinstance(seed, int)
            or isinstance(sample_index, bool)
            or not isinstance(sample_index, int)
            or (seed, sample_index) not in expected_pairs
            or (seed, sample_index) in pairs
        ):
            raise ScientificAdapterError("confidence seed/sample pairs must be unique and complete")
        summary_value = item["upstream_summary"]
        if summary_value is not None:
            summary = _safe_relative_filename(
                summary_value,
                label="confidence upstream summary",
                structure=False,
            )
            if summary in summaries:
                raise ScientificAdapterError("confidence upstream summary filenames must be unique")
            summaries.add(summary)
        structure_item = strict_object(
            item["structure"],
            required=frozenset({"filename", "sha256", "bytes"}),
            label=f"structure confidence results[{index}].structure",
        )
        structure = _safe_relative_filename(
            structure_item["filename"],
            label="confidence structure filename",
            structure=True,
        )
        if structure in paths:
            raise ScientificAdapterError("confidence structure names must be unique safe relative paths")
        digest = structure_item["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ScientificAdapterError("confidence structure digest is invalid")
        size_bytes = structure_item["bytes"]
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or not 1 <= size_bytes <= 2**31:
            raise ScientificAdapterError("confidence structure size is outside the bound")
        metrics = strict_object(
            item["metrics"],
            required=frozenset(),
            optional=frozenset(_METRIC_BOUNDS),
            label=f"structure confidence results[{index}].metrics",
        )
        if not metrics:
            raise ScientificAdapterError("each confidence result requires at least one finite metric")
        for field, value in metrics.items():
            minimum, maximum = _METRIC_BOUNDS[field]
            finite_number(value, minimum=minimum, maximum=maximum, label=field)
        pairs.add((seed, sample_index))
        paths.add(structure)
        results.append(_ConfidenceResult(structure, digest, size_bytes))
    if pairs != expected_pairs:
        raise ScientificAdapterError("confidence seed/sample pairs must be unique and complete")
    return _ConfidenceEnvelope(runtime_id, model_revision, seeds, samples_per_seed, tuple(results))


def collect_structure_outputs(
    workspace: Path,
    *,
    structure_globs: tuple[str, ...],
    confidence_globs: tuple[str, ...],
    manifest_id: str,
    runtime_id: str,
    model_revision: str,
    maximum_structures: int = 256,
) -> CollectedOutput:
    """Collect only allow-listed regular upstream structure/confidence files."""

    structures = sorted({path for pattern in structure_globs for path in workspace.glob(pattern)})
    confidences = sorted({path for pattern in confidence_globs for path in workspace.glob(pattern)})
    if not 1 <= len(structures) <= maximum_structures or len(confidences) != 1:
        raise ScientificAdapterError("upstream output inventory is incomplete or ambiguous")
    output_root = (workspace / "outputs").resolve(strict=True)
    confidence_path = confidences[0].resolve(strict=True)
    if confidence_path != output_root / "confidence.json":
        raise ScientificAdapterError("confidence envelope must be the canonical top-level output")
    envelope = _confidence_envelope(confidence_path.read_bytes(), maximum_structures=maximum_structures)
    if envelope.runtime_id != runtime_id or envelope.model_revision != model_revision:
        raise ScientificAdapterError("confidence envelope execution identity does not match the adapter")
    results = envelope.results
    structures_by_path: dict[PurePosixPath, Path] = {}
    try:
        for path in structures:
            resolved = path.resolve(strict=True)
            if path.is_symlink() or not resolved.is_file():
                raise ScientificAdapterError("structure output must be a regular file")
            structures_by_path[PurePosixPath(resolved.relative_to(output_root).as_posix())] = path
    except (OSError, ValueError) as error:
        raise ScientificAdapterError("structure output escapes the canonical output root") from error
    if len(results) != len(structures_by_path) or {result.structure for result in results} != set(structures_by_path):
        raise ScientificAdapterError("confidence envelope does not bind the exact structure inventory")
    ordered_structures: list[Path] = []
    for result in results:
        path = structures_by_path[result.structure]
        content = path.read_bytes()
        if len(content) != result.size_bytes or hashlib.sha256(content).hexdigest() != result.sha256:
            raise ScientificAdapterError("confidence envelope structure identity does not match its output")
        ordered_structures.append(path)
    entries = [
        (f"prediction.{index}", "protein-structure-mmcif/v1", path, False)
        for index, path in enumerate(ordered_structures, start=1)
    ]
    entries.append(("confidence", "structure-confidence/v1", confidence_path, False))
    return collect_output_files(
        workspace,
        tuple(entries),
        manifest_id=manifest_id,
        maximum_total_bytes=2 * 1024 * 1024 * 1024,
    )


def validate_structure_output(
    manifest: object,
    *,
    artifact_loader: ArtifactLoader,
    expected_structures: int | None,
    validator_id: str,
    backend_id: str,
    model_revision: str,
) -> Mapping[str, object]:
    """Validate canonical structure artifacts without trusting filenames or paths."""

    artifacts = load_output_manifest(
        manifest,
        artifact_loader=artifact_loader,
        maximum_entries=257,
        maximum_total_bytes=2 * 1024 * 1024 * 1024,
    )
    structures = tuple(item for item in artifacts if item.name.startswith("prediction."))
    confidence = tuple(item for item in artifacts if item.name == "confidence")
    if not structures or len(confidence) != 1 or len(structures) + 1 != len(artifacts):
        raise ScientificAdapterError("output manifest must contain predictions and one confidence envelope")
    indexed = {item.name: item for item in structures}
    if set(indexed) != {f"prediction.{index}" for index in range(1, len(structures) + 1)}:
        raise ScientificAdapterError("output predictions are not a contiguous canonical inventory")
    if expected_structures is not None and len(structures) != expected_structures:
        raise ScientificAdapterError("output structure count does not match the request")
    ordered = tuple(indexed[f"prediction.{index}"] for index in range(1, len(structures) + 1))
    atoms = sum(structure_atom_count(item, require_two_chains=False) for item in ordered)
    envelope = _confidence_envelope(confidence[0].content, maximum_structures=256)
    if envelope.runtime_id != backend_id or envelope.model_revision != model_revision:
        raise ScientificAdapterError("confidence envelope execution identity does not match the validator")
    results = envelope.results
    if len(results) != len(ordered):
        raise ScientificAdapterError("confidence envelope cardinality does not match predictions")
    for result, artifact in zip(results, ordered, strict=True):
        if artifact.pointer.sha256 != result.sha256 or artifact.pointer.size_bytes != result.size_bytes:
            raise ScientificAdapterError("confidence envelope does not bind collected prediction identity")
    return {
        "validator_id": validator_id,
        "status": "passed",
        "backend_id": backend_id,
        "structure_count": len(structures),
        "confidence_document_count": 1,
        "atom_count": atoms,
    }
