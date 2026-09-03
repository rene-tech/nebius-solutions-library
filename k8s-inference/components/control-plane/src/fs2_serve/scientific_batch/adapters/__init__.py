"""Registry for model-specific scientific-batch request compilers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from ..models import AdapterExecutionPlan, RuntimeTreeBinding, StageInvocation
from . import boltzgen, localization, proteina_complexa
from .common import ScientificAdapterError, load_json_request, profile_from_catalog
from .localization import (
    ArtifactLocalizationError,
    LocalizationContract,
    LocalizationReceipt,
    load_localization_contracts,
    load_localization_contracts_from_path,
    verify_localized_tree,
)

AdapterCompiler = Callable[[Mapping[str, object], object], AdapterExecutionPlan]


def assert_binding_matches_contract(binding: RuntimeTreeBinding, contract: LocalizationContract) -> None:
    """Reject any drift between a compiled adapter plan and the catalog contract."""

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
    """Fail closed before a stage runs unless every mount is its exact tree.

    Raising rather than returning a rejected receipt is deliberate: the caller
    is about to start model argv that would otherwise read an archive, a partial
    tree, or somebody else's data.
    """

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
    "ArtifactLocalizationError",
    "LocalizationContract",
    "LocalizationReceipt",
    "ScientificAdapterError",
    "boltzgen",
    "compile_adapter_run",
    "load_json_request",
    "load_localization_contracts",
    "load_localization_contracts_from_path",
    "localization",
    "preflight_stage_trees",
    "profile_from_catalog",
    "proteina_complexa",
]
