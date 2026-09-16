#!/usr/bin/env python3
"""Canonical SAI-07 object projection shared by inventory and cleanup.

The projection is deliberately independent of API defaulting noise while
binding every field that affects the legacy cleanup decision.  All optional
fields are present with stable defaults, including ``deletionTimestamp``.
This is the v4 projection; the preserved v3 evidence is never rewritten.
"""

from __future__ import annotations

from typing import Any

PROJECTION_SCHEMA = "fs2-serve.nebius.ai/sai07-object-projection/v4"


class ProjectionError(ValueError):
    """A Kubernetes object cannot be represented by the canonical projection."""


def live_projection(value: dict[str, Any]) -> dict[str, Any]:
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        raise ProjectionError("live object metadata is missing")
    return {
        "projectionSchema": PROJECTION_SCHEMA,
        "apiVersion": value.get("apiVersion"),
        "kind": value.get("kind"),
        "metadata": {
            "name": metadata.get("name"),
            "namespace": metadata.get("namespace", ""),
            "uid": metadata.get("uid"),
            "resourceVersion": metadata.get("resourceVersion"),
            "generation": metadata.get("generation"),
            "deletionTimestamp": metadata.get("deletionTimestamp"),
            "labels": metadata.get("labels", {}),
            "annotations": metadata.get("annotations", {}),
            "ownerReferences": metadata.get("ownerReferences", []),
        },
        "spec": value.get("spec", {}),
        "template": value.get("template", {}),
        "automountServiceAccountToken": value.get("automountServiceAccountToken"),
        "imagePullSecrets": value.get("imagePullSecrets", []),
        "secrets": value.get("secrets", []),
    }
