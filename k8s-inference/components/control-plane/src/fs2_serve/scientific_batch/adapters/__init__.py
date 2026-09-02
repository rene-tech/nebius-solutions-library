"""Registry for model-specific scientific-batch request compilers."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ..models import AdapterExecutionPlan
from . import (
    alphafold3,
    bindcraft,
    boltzgen,
    esmfold2,
    esmfold2_fast,
    freebindcraft,
    mosaic,
    openfold3,
    proteina_complexa,
    proteinmpnn,
    protenix_v2,
    rfdiffusion,
)
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
    """Dispatch through an allow-list and resolve one concrete route variant."""

    request_value = load_json_request(request) if isinstance(request, str | bytes) else request
    if model_id == proteina_complexa.MODEL_ID:
        selected_variant = variant_id
        if selected_variant is None and profile.get("default_variant") is True:
            selected_variant = proteina_complexa.VARIANT_ID
        if selected_variant != proteina_complexa.VARIANT_ID:
            raise ScientificAdapterError("route variant_id does not match the Proteina-Complexa adapter")
        return proteina_complexa.compile_run(profile, request_value, operation_id=operation_id)
    if model_id == boltzgen.MODEL_ID:
        selected_variant = variant_id
        if selected_variant is None and profile.get("default_variant") is True:
            selected_variant = boltzgen.VARIANT_ID
        if selected_variant != boltzgen.VARIANT_ID:
            raise ScientificAdapterError("route variant_id does not match the BoltzGen adapter")
        return boltzgen.compile_run(profile, request_value, operation_id=operation_id)
    selected_variant = variant_id
    if selected_variant is None and profile.get("default_variant") is True:
        profile_variant = profile.get("variant_id")
        selected_variant = profile_variant if isinstance(profile_variant, str) else None
    if model_id == esmfold2.MODEL_ID:
        if selected_variant != esmfold2.VARIANT_ID:
            raise ScientificAdapterError("route variant_id does not match the ESMFold2 adapter")
        return esmfold2.compile_run(profile, request_value, operation_id=operation_id)
    if model_id == esmfold2_fast.MODEL_ID:
        if selected_variant != esmfold2_fast.VARIANT_ID:
            raise ScientificAdapterError("route variant_id does not match the ESMFold2-Fast adapter")
        return esmfold2_fast.compile_run(profile, request_value, operation_id=operation_id)
    if model_id == protenix_v2.MODEL_ID:
        if selected_variant != protenix_v2.VARIANT_ID:
            raise ScientificAdapterError("route variant_id does not match the Protenix v2 adapter")
        return protenix_v2.compile_run(profile, request_value, operation_id=operation_id)
    if model_id == alphafold3.MODEL_ID:
        if selected_variant != alphafold3.VARIANT_ID:
            raise ScientificAdapterError("route variant_id does not match the AlphaFold 3 adapter")
        return alphafold3.compile_run(profile, request_value, operation_id=operation_id)
    if model_id == openfold3.MODEL_ID:
        if selected_variant != openfold3.VARIANT_ID:
            raise ScientificAdapterError("route variant_id does not match the OpenFold3 adapter")
        return openfold3.compile_run(profile, request_value, operation_id=operation_id)
    simple_adapters = {
        freebindcraft.MODEL_ID: (freebindcraft.VARIANT_ID, freebindcraft.compile_run),
        mosaic.MODEL_ID: (mosaic.VARIANT_ID, mosaic.compile_run),
        proteinmpnn.MODEL_ID: (proteinmpnn.VARIANT_ID, proteinmpnn.compile_run),
        rfdiffusion.MODEL_ID: (rfdiffusion.VARIANT_ID, rfdiffusion.compile_run),
    }
    simple = simple_adapters.get(model_id)
    if simple is not None:
        expected_variant, compiler = simple
        if selected_variant != expected_variant:
            raise ScientificAdapterError(f"route variant_id does not match the {model_id} adapter")
        return compiler(profile, request_value, operation_id=operation_id)
    if model_id == bindcraft.MODEL_ID:
        if selected_variant != bindcraft.VARIANT_ID:
            raise ScientificAdapterError("route variant_id does not match the BindCraft adapter")
        return bindcraft.compile_run(profile, request_value, operation_id=operation_id)
    raise ScientificAdapterError(f"no scientific adapter is registered for {model_id}")


__all__ = [
    "AdapterExecutionPlan",
    "ScientificAdapterError",
    "alphafold3",
    "bindcraft",
    "boltzgen",
    "compile_adapter_run",
    "esmfold2",
    "esmfold2_fast",
    "freebindcraft",
    "load_json_request",
    "profile_from_catalog",
    "openfold3",
    "mosaic",
    "proteina_complexa",
    "proteinmpnn",
    "protenix_v2",
    "rfdiffusion",
]
