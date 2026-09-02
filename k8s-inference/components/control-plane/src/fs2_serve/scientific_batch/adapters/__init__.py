"""Registry for model-specific scientific-batch request compilers."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ..models import AdapterExecutionPlan
from . import boltzgen, proteina_complexa
from .common import ScientificAdapterError, load_json_request, profile_from_catalog

AdapterCompiler = Callable[[Mapping[str, object], object], AdapterExecutionPlan]


def compile_adapter_run(
    model_id: str,
    profile: Mapping[str, object],
    request: object,
    *,
    operation_id: str,
    variant_id: str | None = None,
) -> AdapterExecutionPlan:
    """Dispatch through an explicit allow-list; model ID never selects code by path."""

    request_value = load_json_request(request) if isinstance(request, str | bytes) else request
    if model_id == proteina_complexa.MODEL_ID:
        if variant_id is not None and variant_id != proteina_complexa.VARIANT_ID:
            raise ScientificAdapterError("route variant_id does not match the Proteina-Complexa adapter")
        return proteina_complexa.compile_run(profile, request_value, operation_id=operation_id)
    if model_id == boltzgen.MODEL_ID:
        if variant_id is not None and variant_id != boltzgen.VARIANT_ID:
            raise ScientificAdapterError("route variant_id does not match the BoltzGen adapter")
        return boltzgen.compile_run(profile, request_value, operation_id=operation_id)
    raise ScientificAdapterError(f"no scientific adapter is registered for {model_id}")


__all__ = [
    "AdapterExecutionPlan",
    "ScientificAdapterError",
    "boltzgen",
    "compile_adapter_run",
    "load_json_request",
    "profile_from_catalog",
    "proteina_complexa",
]
