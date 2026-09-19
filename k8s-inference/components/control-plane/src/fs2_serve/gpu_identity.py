"""Dependency-light Kubernetes GPU allocation identity shared by readers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

MODEL_ID_LABEL = "fs2-serve.nebius.ai/model-id"
GPU_UUIDS_ANNOTATION = "telemetry.fs2.nebius.ai/gpu-uuids"
GPU_ALLOCATION_OBSERVED_AT_ANNOTATION = "telemetry.fs2.nebius.ai/gpu-allocation-observed-at"
GPU_OBSERVER_RESOLUTION_ANNOTATION = "telemetry.fs2.nebius.ai/gpu-observer-resolution-seconds"
GPU_RESOURCE_PATTERN = re.compile(
    r"^(?:nvidia\.com/(?:gpu|mig-[A-Za-z0-9_.-]+)|amd\.com/gpu|gpu\.intel\.com/(?:i915|xe))$"
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, list) else ()


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value)
    if not text.isdigit():
        return None
    parsed = int(text)
    return parsed if 1 <= parsed <= 64 else None


def pod_gpu_count(pod: Mapping[str, Any]) -> int | None:
    """Count exactly matching GPU requests/limits, without controller imports."""
    total = 0
    containers = _sequence(_mapping(pod.get("spec")).get("containers"))
    if not containers:
        return None
    for raw_container in containers:
        container = _mapping(raw_container)
        resources = _mapping(container.get("resources"))
        requests = _mapping(resources.get("requests"))
        limits = _mapping(resources.get("limits"))
        for name, raw_request in requests.items():
            if not isinstance(name, str) or GPU_RESOURCE_PATTERN.fullmatch(name) is None:
                continue
            request = _positive_int(raw_request)
            limit = _positive_int(limits.get(name))
            if request is None or request != limit:
                return None
            total += request
    return total if 1 <= total <= 64 else None
