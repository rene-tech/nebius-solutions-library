"""Pinned scientific target choices, distinct from filenames and qualification.

The JSON is a projection of the three upstream dictionaries at the adapter's
source revision. A listed configuration is not a live qualification claim.
"""

from __future__ import annotations

import copy
import io
import json
import tarfile
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import zstandard

from .primitives import ScientificParameterError


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(Path(__file__).with_name("proteina_target_catalog.json").read_bytes()))


def public_target_catalog() -> dict[str, Any]:
    """Only include this large catalog in an explicit get_model_schema response."""

    result = copy.deepcopy(_catalog())
    result["selection_help"] = (
        "Choose the exact target_id within the requested variant by target residues, "
        "hotspots, binder length and motif/ligand intent, not by PDB filename. "
        "Upload a target-bundle tar containing the selected bundle_path. "
        "Listing a pinned configuration does not establish runtime or scientific qualification. "
        "Custom target configuration is supported upstream but not yet by this hosted API."
    )
    for variant in result["variants"].values():
        for target_id, target in variant["targets"].items():
            target["description"] = describe_target(target_id, target)
    return result


def describe_target(target_id: str, target: dict[str, Any]) -> str:
    details = [target_id, f"input {target['bundle_path']}"]
    for field, label in (
        ("target_input", "target residues"),
        ("hotspot_residues", "hotspots"),
        ("binder_length", "binder length"),
        ("ligand", "ligand"),
        ("contig_atoms", "fixed motif atoms"),
    ):
        if field in target:
            details.append(f"{label}: {target[field]}")
    return "; ".join(details)


def target_configuration(variant: str, target_id: str) -> dict[str, Any]:
    variants = _catalog()["variants"]
    targets = variants.get(variant, {}).get("targets", {})
    if target_id in targets:
        return copy.deepcopy(targets[target_id])
    # Suggest scientifically distinct choices, never rewrite the request. The
    # normalization compares names only; it cannot determine scientific intent.
    normalized = "".join(character.lower() for character in target_id if character.isalnum())
    candidates = [
        (name, target)
        for name, target in targets.items()
        if normalized and normalized in "".join(character.lower() for character in name if character.isalnum())
    ]
    detail = (
        f"Unsupported Proteina-Complexa target_id {target_id!r} for variant {variant!r}. "
        "target_id is a pinned scientific configuration, not the PDB filename. "
        "Call get_model_schema(model_id='proteina-complexa') and select from target_catalog "
        "for this variant. Custom target configurations are not implemented by the hosted API."
    )
    if candidates:
        detail += " Possible choices (not interchangeable): " + " | ".join(
            describe_target(name, target) for name, target in candidates[:4]
        )
    raise ScientificParameterError("unsupported pinned Proteina target configuration", public_detail=detail)


def validate_target_bundle(
    payload: bytes, *, variant: str, target_id: str, compression: str | None, maximum_bytes: int
) -> None:
    """Inspect caller bytes without extraction; a filename never selects a task."""

    required_path = target_configuration(variant, target_id)["bundle_path"]
    matches = 0
    total = 0
    try:
        source = io.BytesIO(payload)
        stream = zstandard.ZstdDecompressor().stream_reader(source) if compression == "zstd" else source
        with stream, tarfile.open(fileobj=stream, mode="r|gz" if compression == "gzip" else "r|") as archive:
            for member in archive:
                total += member.size
                if member.size < 0 or total > maximum_bytes:
                    raise ValueError("archive exceeds existing model input bound")
                if member.name.removeprefix("./") == required_path:
                    if not member.isfile() or member.size == 0:
                        raise ValueError("required member must be a nonempty regular file")
                    matches += 1
        if matches != 1:
            raise ValueError("required target member missing or duplicated")
    except (tarfile.TarError, zstandard.ZstdError, OSError, EOFError, ValueError) as error:
        raise ScientificParameterError(
            "target bundle does not contain the selected pinned target",
            public_detail=(
                f"Proteina-Complexa target_id {target_id!r} in variant {variant!r} requires exactly one "
                f"nonempty PDB member {required_path!r} inside the target-bundle tar. "
                "The bundle is missing that member, is malformed or exceeds the existing input bound. "
                "Use get_model_schema(model_id='proteina-complexa') target_catalog to inspect the required layout. "
                "The platform does not infer or change the scientific configuration from filenames."
            ),
        ) from error
