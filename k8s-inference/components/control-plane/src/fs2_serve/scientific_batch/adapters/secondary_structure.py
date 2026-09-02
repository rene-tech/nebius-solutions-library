"""Shared bounded output handling for secondary structure adapters."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .common import (
    ArtifactLoader,
    CollectedOutput,
    ScientificAdapterError,
    collect_output_files,
    finite_number,
    load_output_manifest,
    parse_json_artifact,
    structure_atom_count,
)


def collect_structure_outputs(
    workspace: Path,
    *,
    structure_globs: tuple[str, ...],
    confidence_globs: tuple[str, ...],
    manifest_id: str,
    maximum_structures: int = 256,
) -> CollectedOutput:
    """Collect only allow-listed regular upstream structure/confidence files."""

    structures = sorted({path for pattern in structure_globs for path in workspace.glob(pattern)})
    confidences = sorted({path for pattern in confidence_globs for path in workspace.glob(pattern)})
    if not 1 <= len(structures) <= maximum_structures or len(confidences) != 1:
        raise ScientificAdapterError("upstream output inventory is incomplete or ambiguous")
    entries = [
        (f"prediction.{index}", "protein-structure-mmcif/v1", path, False)
        for index, path in enumerate(structures, start=1)
    ]
    entries.append(("confidence", "structure-confidence-json/v1", confidences[0], False))
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
) -> Mapping[str, object]:
    """Validate canonical structure artifacts without trusting filenames or paths."""

    artifacts = load_output_manifest(
        manifest,
        artifact_loader=artifact_loader,
        maximum_entries=258,
        maximum_total_bytes=2 * 1024 * 1024 * 1024,
    )
    structures = tuple(item for item in artifacts if item.name.startswith("prediction."))
    confidence = tuple(item for item in artifacts if item.name == "confidence")
    if not structures or len(confidence) != 1 or len(structures) + 1 != len(artifacts):
        raise ScientificAdapterError("output manifest must contain predictions and one confidence document")
    if expected_structures is not None and len(structures) != expected_structures:
        raise ScientificAdapterError("output structure count does not match the request")
    atoms = sum(structure_atom_count(item, require_two_chains=False) for item in structures)
    metrics = parse_json_artifact(confidence[0].content, label="structure confidence")
    for field, maximum in (("plddt_mean", 100.0), ("ptm", 1.0)):
        if field in metrics:
            finite_number(metrics[field], minimum=0.0, maximum=maximum, label=field)
    samples = metrics.get("samples")
    if samples is not None and (not isinstance(samples, list) or len(samples) != len(structures)):
        raise ScientificAdapterError("confidence sample inventory does not match predictions")
    return {
        "validator_id": validator_id,
        "status": "passed",
        "backend_id": backend_id,
        "structure_count": len(structures),
        "atom_count": atoms,
    }


def write_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
