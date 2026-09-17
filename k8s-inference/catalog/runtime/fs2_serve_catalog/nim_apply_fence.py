"""Canonical, side-effect-free NIM root projections for an apply fence."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any


NIM_ROOT_RESOURCES = {
    ("apps.nvidia.com/v1alpha1", "NIMCache"): "nimcaches",
    ("apps.nvidia.com/v1alpha1", "NIMService"): "nimservices",
}

_VOLATILE_METADATA = {
    "creationTimestamp",
    "deletionGracePeriodSeconds",
    "deletionTimestamp",
    "generation",
    "managedFields",
    "resourceVersion",
    "selfLink",
    "uid",
}


def canonical_nim_root_projection(
    value: Mapping[str, Any], *, require_defaults_closed: bool = True
) -> Mapping[str, Any]:
    """Project the desired NIM root after Kubernetes API defaulting.

    Callers must plan a fully default-closed manifest. API-owned metadata and
    status are excluded. Finalizers are deliberately projected separately: a
    CREATE must have none and an UPDATE must preserve the exact persisted list.
    This prevents operator-added finalizers from changing an otherwise exact
    desired projection without letting the release writer add or remove them.
    """

    api_version = value.get("apiVersion")
    kind = value.get("kind")
    metadata = value.get("metadata")
    if (
        NIM_ROOT_RESOURCES.get((str(api_version), str(kind))) is None
        or not isinstance(metadata, Mapping)
        or not isinstance(metadata.get("namespace"), str)
        or not metadata["namespace"]
        or not isinstance(metadata.get("name"), str)
        or not metadata["name"]
    ):
        raise ValueError("NIM root identity is absent")
    projected = copy.deepcopy(dict(value))
    projected.pop("status", None)
    projected_metadata = projected["metadata"]
    for field in _VOLATILE_METADATA | {"finalizers"}:
        projected_metadata.pop(field, None)
    annotations = projected_metadata.get("annotations")
    if require_defaults_closed and (
        not isinstance(annotations, Mapping)
        or annotations.get("fs2-serve.nebius.ai/apply-fence-defaults-closed") != "true"
    ):
        raise ValueError("NIM root manifest is not explicitly default closed")
    return projected


def canonical_nim_root_finalizers(value: Mapping[str, Any]) -> list[str]:
    metadata = value.get("metadata")
    finalizers = metadata.get("finalizers", []) if isinstance(metadata, Mapping) else None
    if (
        not isinstance(finalizers, list)
        or any(not isinstance(item, str) or not item for item in finalizers)
        or len(finalizers) != len(set(finalizers))
    ):
        raise ValueError("NIM root finalizers are not canonical")
    return sorted(finalizers)
