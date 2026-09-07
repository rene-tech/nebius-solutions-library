"""Interpret existing snapshot supervisor evidence without changing captures.

The measured startup interval includes supervisor scratch/cache preparation
and the CUDA/CRIU restore attempt. It is not a CUDA-copy-only benchmark. The
same boundary is emitted before ordinary loading when restore falls back.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .models import STAGE_CONTAINER_NAME

MAX_LOG_BYTES = 262144


def uses_snapshot_supervisor(pod: Mapping[str, Any]) -> bool:
    spec = pod.get("spec", {})
    if not isinstance(spec, Mapping):
        return False
    for container in spec.get("containers", []):
        if not isinstance(container, Mapping) or container.get("name") != STAGE_CONTAINER_NAME:
            continue
        command = container.get("command", [])
        return (
            isinstance(command, list)
            and "restore" in command
            and any(
                isinstance(argument, str)
                and argument.startswith(("/snapshot-source/", "/snapshot-entrypoint/"))
                for argument in command
            )
        )
    return False


def snapshot_request_started(log: str) -> datetime | None:
    """Read only the first exact supervisor marker from timestamped Pod logs."""
    for line in log.splitlines():
        timestamp, separator, payload = line.partition(" ")
        if not separator or len(payload) > 4096 or '"scientific_snapshot_request"' not in payload:
            continue
        try:
            value = json.loads(payload)
            if not isinstance(value, dict) or value.get("event") != "scientific_snapshot_request":
                continue
            if value.get("mechanism") not in {"cuda-criu-restored", "normal-load-fallback"}:
                continue
            observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if observed.tzinfo is not None:
            return observed
    return None
