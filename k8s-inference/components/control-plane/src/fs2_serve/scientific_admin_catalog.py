"""Scientific model-readiness projection from immutable catalog evidence."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .registry import OperationalModel, Registry, RegistryError
from .scientific_admin import ScientificAdminSourceUnavailableError, ScientificModelSnapshot
from .scientific_admin_models import (
    ScientificAccessGate,
    ScientificBackendIdentity,
    ScientificCachingReadiness,
    ScientificExplicitAlternative,
    ScientificModelReadiness,
    ScientificModelReadinessList,
    ScientificServiceClass,
)
from .scientific_batch.service import ScientificBatchService, ScientificProfileDiscovery

SCIENTIFIC_SOURCE_RECEIPTS = "scientific-source-candidate-receipts.json"
_HYBRID_MODELS = frozenset({"esmfold2", "esmfold2-fast"})
_REFERENCE_DATA_MODELS = frozenset({"alphafold3", "protenix-v2"})
_ALTERNATIVES = {
    "alphafold3": ScientificExplicitAlternative(
        model_id="openfold3",
        display_name="OpenFold3",
        reason="Open alternative; it is not represented as native AlphaFold3.",
    ),
    "bindcraft": ScientificExplicitAlternative(
        model_id="freebindcraft",
        display_name="FreeBindCraft",
        reason="Open alternative; it is not represented as native BindCraft/PyRosetta.",
    ),
}


class ScientificProfileDiscoveryAdapter:
    """Admin projection of tenant-runnable scientific profiles only."""

    def __init__(
        self,
        *,
        scientific_batches: ScientificBatchService | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.scientific_batches = scientific_batches
        self.clock = clock or (lambda: datetime.now(UTC))

    async def list_models(self, *, tenant_id: str | None = None) -> ScientificModelSnapshot:
        profiles = (
            ()
            if self.scientific_batches is None
            else self.scientific_batches.discovery_profiles(
                tenant_id=tenant_id,
                allowed_models=frozenset({"*"}),
                surface="admin",
            )
        )
        return ScientificModelSnapshot(
            data=ScientificModelReadinessList(items=[self._project(profile) for profile in profiles]),
            observed_at=self.clock().astimezone(UTC),
        )

    @staticmethod
    def _project(profile: ScientificProfileDiscovery) -> ScientificModelReadiness:
        service_classes = [ScientificServiceClass(value) for value in profile.service_classes]
        access_profile: Literal["standard", "academic"] = (
            "academic" if profile.access_profile == "academic" else "standard"
        )
        access_state: Literal["not-required", "unverified", "verified", "blocked"] = (
            "verified" if access_profile == "academic" else "not-required"
        )
        execution_mode: Literal["scientific-batch", "hybrid"] = (
            "hybrid" if profile.execution_mode == "hybrid" else "scientific-batch"
        )
        return ScientificModelReadiness(
            model_id=profile.model_id,
            display_name=profile.display_name,
            readiness="qualified",
            readiness_reason=(
                "The exact profile, execution map, access binding, and scheduler eligibility are active."
            ),
            execution_mode=execution_mode,
            batch_supported=True,
            interactive_supported=execution_mode == "hybrid",
            service_classes=service_classes,
            backend=ScientificBackendIdentity(
                backend_id=profile.variant_id,
                kind="qualified-scientific-profile",
                source_repository=profile.source_repository,
                source_revision=profile.source_revision,
                model_revision=profile.source_revision,
                runtime_image_digest=profile.runtime_image_digest,
                execution_identity_digest=profile.execution_identity_sha256,
            ),
            access=ScientificAccessGate(
                profile=access_profile,
                state=access_state,
                gate=(
                    "Deployment-bound tenant academic authorization is verified."
                    if access_profile == "academic"
                    else "No restricted academic asset is required by this backend."
                ),
                receipt_digest=profile.access_receipt_digest,
                alternative=None,
            ),
            caching=ScientificCachingReadiness(
                exact_tier="not-observed",
                image="verified",
                artifacts="verified",
                reference_data="unsupported",
                runtime_checkpoint="unavailable",
                gpu_snapshot="unavailable",
                reason="Discovery does not infer a fast-start tier without an exact observation.",
            ),
        )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScientificAdminSourceUnavailableError(f"scientific candidate {label} is invalid")
    return value


def _text(value: object, label: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ScientificAdminSourceUnavailableError(f"scientific candidate {label} is invalid")
    return value


def _operational(registry: Registry, model_id: str) -> OperationalModel | None:
    try:
        return registry.get(model_id, require_enabled=False)
    except KeyError:
        return None


def _qualified(model: OperationalModel | None) -> bool:
    if model is None or not model.enabled or model.gateway.qualification is None:
        return False
    states = model.gateway.qualification.get("states")
    return isinstance(states, Mapping) and all(
        states.get(key) is True for key in ("runtime_ready", "semantic_qualified", "elasticity_qualified")
    )


class ScientificCatalogFileAdapter:
    """Read candidate identities while upgrading only exact live-qualified routes."""

    def __init__(
        self,
        *,
        registry: Registry,
        receipts_file: Path,
        variants_file: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.registry = registry
        self.receipts_file = receipts_file
        self.variants_file = variants_file or receipts_file.with_name("model-variants.json")
        self.clock = clock or (lambda: datetime.now(UTC))

    def _lane_ids(self) -> dict[str, str]:
        if not self.variants_file.is_file():
            return {}
        try:
            raw = self.variants_file.read_text(encoding="utf-8")
            if len(raw.encode("utf-8")) > 4 * 1024 * 1024:
                raise ScientificAdminSourceUnavailableError("model variant map is too large")
            document = _mapping(json.loads(raw), "model variant map")
            candidates = _mapping(document.get("fallback_candidates"), "model variant candidates")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScientificAdminSourceUnavailableError("model variant map is unavailable") from exc
        lanes: dict[str, str] = {}
        for candidate_id, value in candidates.items():
            if not isinstance(candidate_id, str) or not isinstance(value, Mapping):
                raise ScientificAdminSourceUnavailableError("model variant candidate is invalid")
            lane_id = value.get("lane_id")
            if isinstance(lane_id, str) and 1 <= len(lane_id) <= 128:
                lanes[candidate_id] = lane_id
        return lanes

    async def list_models(self, *, tenant_id: str | None = None) -> ScientificModelSnapshot:
        del tenant_id
        try:
            raw = self.receipts_file.read_text(encoding="utf-8")
            if len(raw.encode("utf-8")) > 1024 * 1024:
                raise ScientificAdminSourceUnavailableError("scientific candidate receipt set is too large")
            document = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScientificAdminSourceUnavailableError("scientific candidate receipts are unavailable") from exc
        root = _mapping(document, "receipt set")
        if root.get("schema") != "fs2-serve.nebius.ai/scientific-source-candidate-receipts/v1":
            raise ScientificAdminSourceUnavailableError("scientific candidate receipt schema is unsupported")
        receipts = root.get("receipts")
        if not isinstance(receipts, list) or len(receipts) > 256:
            raise ScientificAdminSourceUnavailableError("scientific candidate receipt list is invalid")

        lane_ids = self._lane_ids()
        items = [self._project(_mapping(receipt, "receipt"), lane_ids=lane_ids) for receipt in receipts]
        if len({item.model_id for item in items}) != len(items):
            raise ScientificAdminSourceUnavailableError("scientific candidate model identities are duplicated")
        return ScientificModelSnapshot(
            data=ScientificModelReadinessList(items=sorted(items, key=lambda item: item.model_id)),
            observed_at=self.clock().astimezone(UTC),
        )

    def _project(self, receipt: Mapping[str, Any], *, lane_ids: Mapping[str, str]) -> ScientificModelReadiness:
        candidate_id = _text(receipt.get("model_id"), "model_id", 128)
        model_id = lane_ids.get(candidate_id, candidate_id)
        display_name = _text(receipt.get("upstream_name"), "upstream_name", 200)
        source = _mapping(receipt.get("source"), "source")
        repository = _text(source.get("repository"), "source repository")
        source_revision = _text(source.get("revision"), "source revision", 256)
        raw_access_profile = _text(receipt.get("access_profile"), "access profile", 32)
        if raw_access_profile not in {"standard", "academic"}:
            raise ScientificAdminSourceUnavailableError("scientific candidate access profile is unsupported")
        access_profile: Literal["standard", "academic"] = "academic" if raw_access_profile == "academic" else "standard"

        operational = _operational(self.registry, model_id)
        qualified = _qualified(operational)
        academic = access_profile == "academic"
        readiness: Literal["qualified", "candidate", "blocked", "unknown"] = (
            "qualified" if qualified else "blocked" if academic else "candidate"
        )
        access_state: Literal["not-required", "unverified", "verified", "blocked"] = (
            "unverified" if academic else "not-required"
        )
        access = ScientificAccessGate(
            profile=access_profile,
            state=access_state,
            gate=(
                "A verified, non-secret academic asset receipt is required before admission."
                if academic
                else "No restricted academic asset is required by this backend."
            ),
            receipt_digest=None,
            alternative=_ALTERNATIVES.get(model_id),
        )
        runtime_image = operational.gateway.runtime_image_digest if operational is not None else None
        model_revision = operational.gateway.model_revision if operational is not None else None
        execution_mode: Literal["scientific-batch", "hybrid"] = (
            "hybrid" if model_id in _HYBRID_MODELS else "scientific-batch"
        )
        return ScientificModelReadiness(
            model_id=model_id,
            display_name=display_name,
            readiness=readiness,
            readiness_reason=(
                "The exact runtime, semantic validator, and elastic execution evidence are qualified."
                if qualified
                else "Academic asset access is not verified for admission."
                if academic
                else _text(receipt.get("notes"), "notes", 300)
            ),
            execution_mode=execution_mode,
            batch_supported=True,
            interactive_supported=execution_mode == "hybrid",
            service_classes=(
                [
                    ScientificServiceClass.PRESENTATION,
                    ScientificServiceClass.INTERACTIVE,
                    ScientificServiceClass.CUSTOMER_BATCH,
                    ScientificServiceClass.BULK_BACKFILL,
                ]
                if execution_mode == "hybrid"
                else [ScientificServiceClass.CUSTOMER_BATCH, ScientificServiceClass.BULK_BACKFILL]
            ),
            backend=ScientificBackendIdentity(
                backend_id=f"{candidate_id}:native-upstream",
                kind=_text(receipt.get("backend_identity"), "backend identity", 64),
                source_repository=repository,
                source_revision=source_revision,
                model_revision=model_revision,
                runtime_image_digest=runtime_image,
                execution_identity_digest=None,
            ),
            access=access,
            caching=ScientificCachingReadiness(
                exact_tier="not-observed",
                image="verified" if qualified and runtime_image is not None else "candidate",
                artifacts="verified" if qualified and model_revision is not None else "candidate",
                reference_data="candidate" if model_id in _REFERENCE_DATA_MODELS else "unsupported",
                runtime_checkpoint="unavailable",
                gpu_snapshot="unavailable",
                reason="No exact fast-start observation is joined to this scientific backend identity.",
            ),
        )


def scientific_receipts_file(catalog_dir: Path) -> Path:
    """Resolve the immutable receipt set from either supported catalog layout."""

    direct = catalog_dir / "contracts" / SCIENTIFIC_SOURCE_RECEIPTS
    if direct.is_file():
        return direct
    packaged = Path("/opt/fs2/catalog/contracts") / SCIENTIFIC_SOURCE_RECEIPTS
    if packaged.is_file():
        return packaged
    raise RegistryError("scientific source candidate receipts are not installed")
