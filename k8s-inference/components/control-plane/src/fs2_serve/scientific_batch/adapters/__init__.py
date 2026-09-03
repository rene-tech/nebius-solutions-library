"""Allow-listed scientific adapter and collector registry.

Model-owned modules register only compilers and collectors here. The
controller owns dispatch, persistence, artifact transport, and Kubernetes
rendering.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from ..models import (
    PUBLIC_ARTIFACT_ACCESS_CONTEXT,
    AdapterExecutionPlan,
    ArtifactAccessContext,
    RuntimeTreeBinding,
    ScientificInputArtifact,
    StageInvocation,
)
from . import (
    esmfold2,
    esmfold2_fast,
    localization,
    openfold3,
    protenix_v2,
)
from .common import ScientificAdapterError, load_json_request, profile_from_catalog
from .localization import (
    ArtifactLocalizationError,
    LocalizationContract,
    LocalizationReceipt,
    load_localization_contracts,
    load_localization_contracts_from_path,
    verify_localized_tree,
)


@dataclass(frozen=True, slots=True)
class CollectedArtifactFile:
    name: str
    semantic_type: str
    path: Path
    media_type: str
    compression: str | None = None


@dataclass(frozen=True, slots=True)
class CollectedStageOutput:
    artifacts: tuple[CollectedArtifactFile, ...]
    validation: Mapping[str, object]


class CollectionPendingError(RuntimeError):
    """The model process has not atomically published its expected outputs yet."""


AdapterCompiler = Callable[..., AdapterExecutionPlan]
StageCollector = Callable[[StageInvocation, Path], CollectedStageOutput]

_COMPILERS: dict[str, AdapterCompiler] = {}
_COLLECTORS: dict[str, StageCollector] = {}
_COLLECTORS_BY_MODEL: dict[str, frozenset[str]] = {}
_DEFAULT_VARIANTS: dict[str, str] = {}


def assert_binding_matches_contract(binding: RuntimeTreeBinding, contract: LocalizationContract) -> None:
    """Reject drift between a compiled adapter plan and a localization contract."""

    if binding.artifact_id != contract.artifact_id:
        raise ArtifactLocalizationError("runtime tree binding names a different artifact than its contract")
    if binding.mount_path not in contract.tree.mount_paths:
        raise ArtifactLocalizationError(f"{binding.artifact_id} mount path drifted from the localization contract")
    if binding.archive_sha256 != contract.archive.sha256:
        raise ArtifactLocalizationError(f"{binding.artifact_id} archive provenance drifted from the contract")
    if binding.tree_inventory_sha256 != contract.tree.inventory_sha256:
        raise ArtifactLocalizationError(f"{binding.artifact_id} extracted-tree identity drifted from the contract")
    if binding.entry_count != contract.tree.entry_count:
        raise ArtifactLocalizationError(f"{binding.artifact_id} tree entry count drifted from the contract")


def preflight_stage_trees(
    invocation: StageInvocation,
    mounts: Mapping[str, Path],
    contracts: Mapping[str, LocalizationContract],
    *,
    now: datetime | None = None,
    verify_probes: bool = True,
    observation: Mapping[str, object] | None = None,
) -> tuple[LocalizationReceipt, ...]:
    """Fail closed unless every runtime tree is mounted at its exact identity."""

    receipts: list[LocalizationReceipt] = []
    for binding in invocation.runtime_trees:
        contract = contracts.get(binding.artifact_id)
        if contract is None:
            raise ArtifactLocalizationError(f"no localization contract is registered for {binding.artifact_id}")
        assert_binding_matches_contract(binding, contract)
        mount = mounts.get(binding.artifact_id)
        if mount is None:
            raise ArtifactLocalizationError(f"{binding.artifact_id} was not mounted for stage {invocation.stage_id}")
        receipt = verify_localized_tree(
            mount,
            contract,
            now=now,
            verify_probes=verify_probes,
            observation=observation,
        )
        if not receipt.verified:
            raise ArtifactLocalizationError(
                f"stage {invocation.stage_id} refused {binding.artifact_id}: {receipt.rejection_reason}"
            )
        receipts.append(receipt)
    return tuple(receipts)


def register_adapter(
    *,
    model_id: str,
    compiler: AdapterCompiler,
    collectors: Mapping[str, StageCollector],
) -> None:
    existing_collectors = _COLLECTORS_BY_MODEL.get(model_id, frozenset())
    if existing_collectors or not collectors or any(key in _COLLECTORS for key in collectors):
        raise RuntimeError("scientific adapter or collector is already registered")
    _COMPILERS[model_id] = compiler
    _COLLECTORS.update(collectors)
    _COLLECTORS_BY_MODEL[model_id] = frozenset(collectors)


def _register_legacy_primary(module_name: str) -> None:
    """Retain compiler discovery for primary modules carried by adapter branches."""

    try:
        module = importlib.import_module(f"{__name__}.{module_name}")
    except ModuleNotFoundError as error:
        if error.name == f"{__name__}.{module_name}":
            return
        raise
    model_id = module.MODEL_ID
    variant = module.VARIANT_ID
    compile_run = module.compile_run

    def compiler(
        profile: Mapping[str, object],
        request: object,
        *,
        operation_id: str,
        variant_id: str,
        access_context: ArtifactAccessContext,
        input_artifacts: tuple[ScientificInputArtifact, ...],
    ) -> AdapterExecutionPlan:
        del access_context, input_artifacts
        if variant_id != variant:
            raise ScientificAdapterError(f"route variant_id does not match the {model_id} adapter")
        return cast(AdapterExecutionPlan, compile_run(profile, request, operation_id=operation_id))

    _COMPILERS.setdefault(model_id, compiler)
    _DEFAULT_VARIANTS.setdefault(model_id, variant)
    globals()[module_name] = module


def compile_adapter_run(
    model_id: str,
    profile: Mapping[str, object],
    request: object,
    *,
    operation_id: str,
    variant_id: str | None = None,
    access_context: ArtifactAccessContext = PUBLIC_ARTIFACT_ACCESS_CONTEXT,
    input_artifacts: tuple[ScientificInputArtifact, ...] = (),
) -> AdapterExecutionPlan:
    request_value = load_json_request(request) if isinstance(request, str | bytes) else request
    try:
        compiler = _COMPILERS[model_id]
    except KeyError as error:
        raise ValueError(f"no scientific adapter is registered for {model_id}") from error
    return compiler(
        profile,
        request_value,
        operation_id=operation_id,
        variant_id=variant_id or _DEFAULT_VARIANTS.get(model_id, "default"),
        access_context=access_context,
        input_artifacts=input_artifacts,
    )


def collect_stage_output(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput:
    try:
        collector = _COLLECTORS[invocation.collector_id]
    except KeyError as error:
        raise ValueError(f"no scientific collector is registered for {invocation.collector_id}") from error
    output = collector(invocation, workspace)
    if not isinstance(output, CollectedStageOutput):
        raise TypeError("scientific collector returned another controller type")
    if output.validation.get("validator_id") != invocation.validator_id:
        raise ValueError("scientific validation identity differs from the stage invocation")
    return output


for _module_name in (
    "proteina_complexa",
    "boltzgen",
    "bindcraft",
    "esmfold2",
    "esmfold2_fast",
    "protenix_v2",
    "openfold3",
):
    _register_legacy_primary(_module_name)


__all__ = [
    "AdapterCompiler",
    "ArtifactLocalizationError",
    "CollectedArtifactFile",
    "CollectedStageOutput",
    "CollectionPendingError",
    "LocalizationContract",
    "LocalizationReceipt",
    "ScientificAdapterError",
    "StageCollector",
    "collect_stage_output",
    "compile_adapter_run",
    "esmfold2",
    "esmfold2_fast",
    "load_localization_contracts",
    "load_localization_contracts_from_path",
    "load_json_request",
    "localization",
    "openfold3",
    "preflight_stage_trees",
    "profile_from_catalog",
    "register_adapter",
    "protenix_v2",
]

for _module_name in ("proteina_complexa", "boltzgen", "bindcraft"):
    if _module_name in globals():
        __all__.append(_module_name)
