"""Error types and strict object parsing shared by every scientific adapter.

This module deliberately depends on nothing but the standard library. The
localization contract is enforced identically inside the control plane and
inside a staging Job that runs in a model runtime image, and both need these
definitions without dragging in the controller, the catalog adapter, or PyYAML.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast


class ScientificAdapterError(ValueError):
    """A public request or resolved artifact violates the adapter contract."""


class ArtifactLocalizationError(ScientificAdapterError):
    """A runtime mount, archive, or localization contract failed closed."""


class TreeBoundExceededError(ArtifactLocalizationError):
    """A mount holds more entries or bytes than the contract can account for.

    Kept distinct from an unsafe entry so a caller can report "this is not the
    contracted tree" rather than "this tree is dangerous"; scanning stops at the
    bound either way, so a hostile mount can never be walked without limit.
    """


def strict_object(
    value: object,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    label: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ScientificAdapterError(f"{label} must be an object")
    item = cast(Mapping[str, object], value)
    fields = set(item)
    missing = sorted(required - fields)
    unknown = sorted(fields - required - optional)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise ScientificAdapterError(f"{label} fields are invalid: {', '.join(details)}")
    return item


__all__ = [
    "ArtifactLocalizationError",
    "ScientificAdapterError",
    "TreeBoundExceededError",
    "strict_object",
]
