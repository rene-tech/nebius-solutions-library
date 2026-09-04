"""Verified inner-manifest input selection for production scientific adapters."""

from __future__ import annotations

from ..models import ScientificInputArtifact
from .common import PublicRunRequest
from .primitives import ScientificAdapterError

SCIENTIFIC_MANIFEST_MEDIA_TYPE = "application/vnd.fs2.scientific-manifest+json"


def verified_manifest_entry(
    request: PublicRunRequest,
    input_artifacts: tuple[ScientificInputArtifact, ...] | None,
    *,
    logical_artifact_id: str,
    semantic_type: str,
    media_type: str,
    compressions: frozenset[str | None],
    maximum_bytes: int,
    label: str,
) -> ScientificInputArtifact:
    """Select one artifact-service-verified payload, never the outer manifest."""

    if request.input_manifest.media_type != SCIENTIFIC_MANIFEST_MEDIA_TYPE:
        raise ScientificAdapterError(f"{label} input_manifest must identify a scientific manifest")
    if request.input_manifest.compression not in {None, "none"}:
        raise ScientificAdapterError(f"{label} input manifest must not be compressed")
    matches = (
        ()
        if input_artifacts is None
        else tuple(item for item in input_artifacts if item.logical_artifact_id == logical_artifact_id)
    )
    if len(matches) != 1 or input_artifacts is None or len(input_artifacts) != 1:
        raise ScientificAdapterError(
            f"{label} input manifest must contain exactly one verified {logical_artifact_id} entry"
        )
    item = matches[0]
    if item.semantic_type != semantic_type:
        raise ScientificAdapterError(f"{label} {logical_artifact_id} semantic type is invalid")
    if item.media_type != media_type or item.compression not in compressions:
        raise ScientificAdapterError(f"{label} {logical_artifact_id} media or compression type is invalid")
    if not 1 <= item.size_bytes <= maximum_bytes:
        raise ScientificAdapterError(f"{label} {logical_artifact_id} size is outside the adapter bound")
    if str(item.artifact_id) == request.input_manifest.artifact_id:
        raise ScientificAdapterError(f"{label} payload and manifest must be distinct artifacts")
    return item


__all__ = ["SCIENTIFIC_MANIFEST_MEDIA_TYPE", "verified_manifest_entry"]
