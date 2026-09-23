"""Discover and validate public manifest roles from executable adapter constants.

Payload names and MIME types are part of the model interface, not labels an
agent should invent. This projection does not change model recipes or inputs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .adapters import (
    alphafold3,
    amber,
    bindcraft,
    boltzgen,
    esmfold2,
    esmfold2_fast,
    gromacs,
    lammps,
    mosaic,
    namd,
    openfold3,
    proteina_complexa,
    protenix_v2,
    rfdiffusion,
    video_augmentation,
)
from .adapters.cosmos_lerobot import public_input_contract as lerobot_input_contract
from .models import ScientificInputArtifact
from .profile_catalog import ScientificRequestError


def _entry(
    name: str, semantic: str, media: str, maximum: int, compressions: tuple[str, ...] = ("none",)
) -> dict[str, Any]:
    return {
        "name": name,
        "semantic_type": semantic,
        "media_type": media,
        "compression": compressions[0],
        "allowed_compressions": list(compressions),
        "maximum_bytes": maximum,
    }


def public_input_contract(model_id: str) -> dict[str, Any] | None:
    """Return a fresh, caller-visible descriptor; never infer by model name."""
    if model_id in {gromacs.MODEL_ID, "gromacs-mpi"}:
        return gromacs.public_input_contract()
    for engine in (lammps, namd, amber):
        if model_id == engine.MODEL_ID:
            return engine.public_input_contract()
    if model_id == video_augmentation.MODEL_ID:
        return video_augmentation.public_input_contract()
    if model_id == "cosmos3-lerobot-augmentation":
        return lerobot_input_contract()
    entry = None
    for module in (esmfold2, esmfold2_fast, openfold3, protenix_v2):
        if model_id == module.MODEL_ID:
            entry = _entry(
                module.INPUT_ARTIFACT_ID, module.INPUT_SEMANTIC_TYPE, module.INPUT_MEDIA_TYPE, module.MAX_INPUT_BYTES
            )
    for target in (mosaic, bindcraft):
        if model_id == target.MODEL_ID:
            entry = _entry(
                target.TARGET_INPUT_ID, target.TARGET_SEMANTIC_TYPE, target.TARGET_MEDIA_TYPE, target.MAX_INPUT_BYTES
            )
    if model_id == alphafold3.MODEL_ID:
        entry = _entry(
            alphafold3.FOLD_INPUT_ID,
            alphafold3.FOLD_INPUT_SEMANTIC_TYPE,
            alphafold3.FOLD_INPUT_MEDIA_TYPE,
            alphafold3.MAX_INPUT_BYTES,
        )
    if model_id == boltzgen.MODEL_ID:
        entry = _entry(
            boltzgen.CAMPAIGN_INPUT_ID,
            boltzgen.CAMPAIGN_INPUT_SEMANTIC_TYPE,
            boltzgen.CAMPAIGN_INPUT_MEDIA_TYPE,
            boltzgen.MAX_INPUT_BYTES,
            ("gzip",),
        )
    if model_id == proteina_complexa.MODEL_ID:
        entry = _entry(
            proteina_complexa.TARGET_BUNDLE_ID,
            proteina_complexa.TARGET_BUNDLE_SEMANTIC_TYPE,
            proteina_complexa.TARGET_BUNDLE_MEDIA_TYPE,
            proteina_complexa.MAX_INPUT_BYTES,
            ("gzip", "zstd"),
        )
    common = {
        "exactly_one_entry": True,
        "entry_name_rule": "Entry names are the logical roles below, not filenames or experiment names.",
        "manifest_media_type": "application/vnd.fs2.scientific-manifest+json",
        "manifest_compression": "none",
    }
    if model_id == rfdiffusion.MODEL_ID:
        return {
            **common,
            "operation_parameter": "operation",
            "operations": {
                "design-backbone": {
                    **_entry(
                        rfdiffusion.DESIGN_INPUT_ID, rfdiffusion.DESIGN_INPUT_SEMANTIC_TYPE, "text/plain", 64 * 1024
                    ),
                    "description": (
                        "A nonempty UTF-8 plain-text note recording the requested unconditional design. "
                        "This artifact is retained for provenance; its text is not parsed as executable "
                        "constraints and does not configure inference. The typed parameters (contigs, "
                        "num_designs, seed, diffuser_T and optional length) are authoritative. "
                        "No target PDB, motif spans or hotspot residues are used for this operation."
                    ),
                    "example_content": "RFdiffusion unconditional backbone length 76\n",
                },
                "scaffold-motif": {
                    **_entry(
                        rfdiffusion.TARGET_INPUT_ID,
                        rfdiffusion.TARGET_INPUT_SEMANTIC_TYPE,
                        "chemical/x-pdb",
                        rfdiffusion.MAX_INPUT_BYTES,
                    ),
                    "description": (
                        "The actual target PDB containing the motif residues named by the typed contigs. "
                        "Unlike the unconditional design note, these structure coordinates are used in inference."
                    ),
                },
            },
        }
    return {**common, "entry": entry} if entry is not None else None


def validate_input_roles(
    model_id: str, request: Mapping[str, Any], entries: tuple[ScientificInputArtifact, ...]
) -> None:
    """Reject caller metadata before compiling a runtime plan or admitting work."""
    if model_id == video_augmentation.MODEL_ID:
        try:
            video_augmentation.validate_entries(request, entries)
        except video_augmentation.ScientificAdapterError as error:
            raise ScientificRequestError("video input artifact roles are invalid", public_detail=str(error)) from error
        return
    contract = public_input_contract(model_id)
    if contract is None:
        return
    expected = contract.get("entry")
    if "operations" in contract:
        expected = contract["operations"].get(request.get("operation"))
    if "source_kinds" in contract:
        source = request.get("parameters", {}).get("source", {})
        expected = contract["source_kinds"].get(source.get("kind"))
    if expected is None:
        # Parameter-schema validation owns unknown operation/source selectors.
        return
    allowed = expected.get("allowed_compressions", [expected["compression"]])
    fields = []
    if len(entries) != 1:
        fields.append("entry count")
    else:
        item = entries[0]
        for field, actual in (
            ("name", item.logical_artifact_id),
            ("semantic_type", item.semantic_type),
            ("media_type", item.media_type),
        ):
            if actual != expected[field]:
                fields.append(field)
        if (item.compression or "none") not in allowed:
            fields.append("compression")
        if not 1 <= item.size_bytes <= expected["maximum_bytes"]:
            fields.append("size_bytes")
        if str(item.artifact_id) == request["input_manifest"]["artifact_id"]:
            fields.append("distinct payload artifact")
    if fields:
        detail = (
            f"Input manifest violates the model contract ({', '.join(fields)}). "
            f"Provide exactly one entry named '{expected['name']}', semantic_type "
            f"'{expected['semantic_type']}', payload media_type '{expected['media_type']}', "
            f"compression {allowed}, and 1..{expected['maximum_bytes']} bytes. "
            "The outer manifest has a different media type: "
            "application/vnd.fs2.scientific-manifest+json. Read input_artifact_contract "
            "from get_model_schema. Reuse verified payloads only if their metadata already matches; "
            "otherwise upload with the correct metadata. No operation was admitted."
        )
        raise ScientificRequestError("scientific input artifact roles are invalid", public_detail=detail)
