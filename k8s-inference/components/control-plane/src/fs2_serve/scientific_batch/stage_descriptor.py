"""Bounded, immutable launch metadata outside the Pod argv/environment.

Only metadata travels through this descriptor. Native files still stream from
object storage with their individually verified identities. The descriptor is
rebuilt from the admitted plan, not the current catalog or caller input.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from .capability import CapabilityArtifact
from .models import StageInvocation

SCHEMA = "fs2-serve.nebius.ai/scientific-stage-descriptor/v1"
RELATIVE_PATH = ".fs2/stage-descriptor.json"
MAX_BYTES = 32 * 1024**2


def invocation_json(invocation: StageInvocation) -> str:
    value = asdict(invocation)
    # This pre-existing companion contract has never used runtime_trees;
    # immutable localization remains in its separate runtime marker.
    value.pop("runtime_trees")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def descriptor_bytes(invocation: StageInvocation, artifacts: Sequence[CapabilityArtifact]) -> bytes:
    by_id = {item.logical_artifact_id: item for item in artifacts}
    if len(by_id) != len(artifacts) or set(by_id) != set(invocation.consumes):
        raise ValueError("stage descriptor input identities differ from the invocation")
    materializations = []
    for item in invocation.materializations:
        source = by_id[item.artifact_id]
        materializations.append(
            {
                "logical_artifact_id": item.artifact_id,
                "artifact_id": str(source.artifact_id),
                "destination": item.destination,
                "mode": item.mode.value,
                "compression": item.compression,
                "yaml_name": item.yaml_name,
                "reuse_prefix": item.reuse_prefix,
                "expected_digest": source.digest,
                "expected_size_bytes": source.size_bytes,
                "expected_media_type": source.media_type,
            }
        )
    content = json.dumps(
        {
            "schema": SCHEMA,
            "invocation": json.loads(invocation_json(invocation)),
            "materializations": materializations,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    if len(content) > MAX_BYTES:
        raise ValueError("stage descriptor exceeds its metadata byte bound")
    return content


def verified_descriptor(content: bytes, expected_sha256: str) -> dict[str, Any]:
    if not 0 < len(content) <= MAX_BYTES or hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("stage descriptor differs from its frozen identity")
    value: dict[str, Any] = json.loads(content)
    if set(value) != {"schema", "invocation", "materializations"} or value["schema"] != SCHEMA:
        raise ValueError("stage descriptor schema is invalid")
    if not isinstance(value["invocation"], dict) or not isinstance(value["materializations"], list):
        raise ValueError("stage descriptor fields are invalid")
    return value
