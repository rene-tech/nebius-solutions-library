"""Allow-listed scientific adapter and collector registry.

Model-owned modules register only compilers and collectors here. The
controller owns dispatch, persistence, artifact transport, and Kubernetes
rendering.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from ..models import (
    AdapterExecutionPlan,
    ArtifactAccessContext,
    RuntimeTreeBinding,
    ScientificInputArtifact,
    StageInvocation,
)
from . import bindcraft, boltzgen, localization, proteina_complexa
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


@dataclass(frozen=True, slots=True)
class StageExecutionContract:
    """Static adapter facts used to build the trusted deployment execution map."""

    collector_id: str
    validator_id: str
    runtime_artifacts: tuple[str, ...]


class CollectionPendingError(RuntimeError):
    """The model process has not atomically published its expected outputs yet."""


AdapterCompiler = Callable[..., AdapterExecutionPlan]
StageCollector = Callable[[StageInvocation, Path], CollectedStageOutput]

_COMPILERS: dict[str, AdapterCompiler] = {}
_COLLECTORS: dict[str, StageCollector] = {}
_STAGE_CONTRACTS: dict[str, Mapping[str, StageExecutionContract]] = {}


def assert_binding_matches_contract(binding: RuntimeTreeBinding, contract: LocalizationContract) -> None:
    """Reject drift between a primary candidate plan and its tree contract."""

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
    """Fail closed unless each legacy primary tree is exactly localized."""

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
    stage_contracts: Mapping[str, StageExecutionContract] | None = None,
) -> None:
    if (
        model_id in _COMPILERS
        or not collectors
        or any(key in _COLLECTORS for key in collectors)
        or (
            stage_contracts is not None
            and any(contract.collector_id not in collectors for contract in stage_contracts.values())
        )
    ):
        raise RuntimeError("scientific adapter or collector is already registered")
    _COMPILERS[model_id] = compiler
    _COLLECTORS.update(collectors)
    if stage_contracts is not None:
        _STAGE_CONTRACTS[model_id] = MappingProxyType(dict(stage_contracts))


def stage_execution_contracts(model_id: str) -> Mapping[str, StageExecutionContract]:
    """Return the packaged adapter's immutable stage and artifact closure."""

    try:
        return _STAGE_CONTRACTS[model_id]
    except KeyError as error:
        raise ValueError(f"no scientific adapter is registered for {model_id}") from error


def compile_adapter_run(
    model_id: str,
    profile: Mapping[str, object],
    request: object,
    *,
    operation_id: str,
    variant_id: str | None = None,
    access_context: ArtifactAccessContext | None = None,
    input_artifacts: tuple[ScientificInputArtifact, ...] = (),
) -> AdapterExecutionPlan:
    request_value = load_json_request(request) if isinstance(request, str | bytes) else request
    if model_id == proteina_complexa.MODEL_ID:
        if variant_id is not None and variant_id != proteina_complexa.VARIANT_ID:
            raise ScientificAdapterError("route variant_id does not match the Proteina-Complexa adapter")
        return proteina_complexa.compile_run(profile, request_value, operation_id=operation_id)
    if model_id == boltzgen.MODEL_ID:
        if variant_id is not None and variant_id != boltzgen.VARIANT_ID:
            raise ScientificAdapterError("route variant_id does not match the BoltzGen adapter")
        return boltzgen.compile_run(profile, request_value, operation_id=operation_id)
    if model_id == bindcraft.MODEL_ID:
        if variant_id is not None and variant_id != bindcraft.VARIANT_ID:
            raise ScientificAdapterError("route variant_id does not match the BindCraft adapter")
        return bindcraft.compile_run(profile, request_value, operation_id=operation_id)
    if variant_id is None or access_context is None:
        raise ScientificAdapterError("controller-owned variant and artifact access context are required")
    try:
        compiler = _COMPILERS[model_id]
    except KeyError as error:
        raise ValueError(f"no scientific adapter is registered for {model_id}") from error
    return compiler(
        profile,
        request_value,
        operation_id=operation_id,
        variant_id=variant_id,
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


__all__ = [
    "AdapterCompiler",
    "CollectedArtifactFile",
    "CollectedStageOutput",
    "CollectionPendingError",
    "ArtifactLocalizationError",
    "LocalizationContract",
    "LocalizationReceipt",
    "ScientificAdapterError",
    "bindcraft",
    "boltzgen",
    "StageCollector",
    "StageExecutionContract",
    "collect_stage_output",
    "compile_adapter_run",
    "load_json_request",
    "load_localization_contracts",
    "load_localization_contracts_from_path",
    "localization",
    "preflight_stage_trees",
    "profile_from_catalog",
    "boltzgen",
    "proteina_complexa",
    "register_adapter",
    "stage_execution_contracts",
]


# Model modules are imported only after the registry types exist, avoiding a
# second scheduler protocol while keeping model-specific code in unique files.
from . import alphafold3, esmfold2, esmfold2_fast, openfold3, protenix_v2  # noqa: E402

register_adapter(
    model_id=esmfold2.MODEL_ID,
    compiler=esmfold2.compile_run,
    collectors={
        "esmfold2-prepare-collector-v1": esmfold2.collect_prepare,
        "esmfold2-result-collector-v1": esmfold2.collect_result,
    },
    stage_contracts=esmfold2.STAGE_EXECUTION_CONTRACTS,
)
register_adapter(
    model_id=esmfold2_fast.MODEL_ID,
    compiler=esmfold2_fast.compile_run,
    collectors={
        "esmfold2-fast-prepare-collector-v1": esmfold2_fast.collect_prepare,
        "esmfold2-fast-result-collector-v1": esmfold2_fast.collect_result,
    },
    stage_contracts=esmfold2_fast.STAGE_EXECUTION_CONTRACTS,
)
register_adapter(
    model_id=alphafold3.MODEL_ID,
    compiler=alphafold3.compile_run,
    collectors={
        "alphafold3-data-collector-v1": alphafold3.collect_data,
        "alphafold3-result-collector-v1": alphafold3.collect_result,
    },
    stage_contracts=alphafold3.STAGE_EXECUTION_CONTRACTS,
)
register_adapter(
    model_id=openfold3.MODEL_ID,
    compiler=openfold3.compile_run,
    collectors={
        "openfold3-data-collector-v1": openfold3.collect_data,
        "openfold3-result-collector-v1": openfold3.collect_result,
    },
    stage_contracts=openfold3.STAGE_EXECUTION_CONTRACTS,
)
register_adapter(
    model_id=protenix_v2.MODEL_ID,
    compiler=protenix_v2.compile_run,
    collectors={
        "protenix-v2-prep-collector-v1": protenix_v2.collect_prep,
        "protenix-v2-result-collector-v1": protenix_v2.collect_result,
    },
    stage_contracts=protenix_v2.STAGE_EXECUTION_CONTRACTS,
)
