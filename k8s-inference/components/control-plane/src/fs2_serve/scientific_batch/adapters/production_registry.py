"""Install the reviewed secondary and academic adapters into one global registry.

The existing qualified primary models retain their frozen recipe identity: this
bootstrap is loaded by the production execution/companion path, while candidate
models remain route-disabled until their own complete receipts are published.
"""

from __future__ import annotations

from collections.abc import Mapping

from . import (
    _COLLECTORS,
    _COLLECTORS_BY_MODEL,
    _COMPILERS,
    _DEFAULT_VARIANTS,
    AdapterCompiler,
    StageCollector,
    bindcraft,
    boltzgen,
    esmfold2,
    esmfold2_fast,
    mosaic,
    openfold3,
    proteina_complexa,
    protenix_v2,
    rfdiffusion,
)

_INSTALLED = False


def _secondary_collectors() -> Mapping[str, tuple[AdapterCompiler, str, Mapping[str, StageCollector]]]:
    result: dict[str, tuple[AdapterCompiler, str, Mapping[str, StageCollector]]] = {}
    for module in (esmfold2, esmfold2_fast, protenix_v2, openfold3):
        collector = module.collect_companion_output
        result[module.MODEL_ID] = (
            _COMPILERS[module.MODEL_ID],
            module.VARIANT_ID,
            {str(contract["collector_id"]): collector for contract in module.STAGE_EXECUTION_CONTRACTS.values()},
        )
    return result


def _primary_collectors() -> Mapping[str, tuple[AdapterCompiler, str, Mapping[str, StageCollector]]]:
    """Return only primary models carried by the production execution map.

    Every primary compiler with a production collector is installed through
    this one bootstrap. Route exposure is still controlled independently by
    the qualified workload profile and execution map.
    """

    return {
        proteina_complexa.MODEL_ID: (
            _COMPILERS[proteina_complexa.MODEL_ID],
            proteina_complexa.VARIANT_ID,
            {
                proteina_complexa.COLLECTOR_ID: proteina_complexa.collect_companion_output,
            },
        ),
        boltzgen.MODEL_ID: (
            _COMPILERS[boltzgen.MODEL_ID],
            boltzgen.VARIANT_ID,
            {"boltzgen-v0-3-2": boltzgen.collect_companion_output},
        ),
        bindcraft.MODEL_ID: (
            _COMPILERS[bindcraft.MODEL_ID],
            bindcraft.VARIANT_ID,
            {
                bindcraft.DESIGN_COLLECTOR_ID: bindcraft.collect_companion_output,
                bindcraft.AGGREGATE_COLLECTOR_ID: bindcraft.collect_companion_output,
            },
        ),
        mosaic.MODEL_ID: (
            _COMPILERS[mosaic.MODEL_ID],
            mosaic.VARIANT_ID,
            {mosaic.COLLECTOR_ID: mosaic.collect_companion_output},
        ),
        rfdiffusion.MODEL_ID: (
            _COMPILERS[rfdiffusion.MODEL_ID],
            rfdiffusion.VARIANT_ID,
            {rfdiffusion.COLLECTOR_ID: rfdiffusion.collect_companion_output},
        ),
    }


def install_production_adapters() -> None:
    """Register the closed production allow-list exactly once per process."""

    global _INSTALLED
    if _INSTALLED:
        return

    # AlphaFold 3 imports the canonical collection record types from the parent
    # registry, so it must be imported only after that registry is initialized.
    from . import alphafold3

    registrations = dict(_primary_collectors())
    registrations.update(_secondary_collectors())
    registrations[alphafold3.MODEL_ID] = (
        alphafold3.compile_run,
        alphafold3.VARIANT_ID,
        {
            alphafold3.DATA_COLLECTOR_ID: alphafold3.collect_data,
            alphafold3.RESULT_COLLECTOR_ID: alphafold3.collect_result,
        },
    )
    collector_ids = [collector_id for _, _, collectors in registrations.values() for collector_id in collectors]
    if len(collector_ids) != len(set(collector_ids)) or any(
        collector_id in _COLLECTORS for collector_id in collector_ids
    ):
        raise RuntimeError("production scientific collector identity is duplicated")
    if any(_COLLECTORS_BY_MODEL.get(model_id) for model_id in registrations):
        raise RuntimeError("production scientific adapter was registered outside the bootstrap")
    if any(model_id not in _COMPILERS for model_id in registrations if model_id != alphafold3.MODEL_ID):
        raise RuntimeError("production scientific collector has no registered compiler")
    if alphafold3.MODEL_ID in _COMPILERS:
        raise RuntimeError("AlphaFold 3 compiler was registered outside the production bootstrap")

    for model_id, (compiler, variant_id, collectors) in registrations.items():
        _COMPILERS[model_id] = compiler
        _DEFAULT_VARIANTS[model_id] = variant_id
        _COLLECTORS.update(collectors)
        _COLLECTORS_BY_MODEL[model_id] = frozenset(collectors)
    _INSTALLED = True


__all__ = ["install_production_adapters"]
