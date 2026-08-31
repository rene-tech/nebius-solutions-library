"""Gateway facade over the models lane's canonical consumer contract."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from fs2_serve_catalog.consumer import (
    GatewayCatalog,
    GatewayModel,
    ServingBinding,
    bind_gateway_catalog,
    load_serving_bindings,
)
from fs2_serve_catalog.loader import CatalogError, load_catalog
from fs2_serve_catalog.variant_promotions import VariantGatewayCatalog, load_variant_gateway_catalog

from .lean_routes import LeanRouteError, bind_lean_routes


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ProbeSpec:
    method: str
    path: str
    expected_status: int
    timeout_seconds: float


@dataclass(frozen=True)
class OperationalModel:
    """Canonical gateway projection plus operational fields from the same base record."""

    gateway: GatewayModel
    max_attempts: int
    max_gpu_seconds_per_attempt: float
    retry_base_seconds: float
    variant_id: str | None = None
    variant_digest: str | None = None
    variant_valid_until: str | None = None
    lean_static: bool = False

    @property
    def id(self) -> str:
        return self.gateway.model_id

    @property
    def enabled(self) -> bool:
        return self.gateway.routable

    @property
    def binding(self) -> ServingBinding:
        if self.gateway.binding is None:
            raise RegistryError("model has no live serving binding")
        return self.gateway.binding

    def _probe(self, field: str, *, required: bool) -> ProbeSpec | None:
        value = self.gateway.scale_contract.to_dict().get(field)
        if value is None:
            if required:
                raise RegistryError(f"routable model has no {field} contract")
            return None
        if not isinstance(value, dict):
            raise RegistryError(f"canonical {field} contract is invalid")
        try:
            return ProbeSpec(
                method=str(value["method"]),
                path=str(value["path"]),
                expected_status=int(value["expected_status"]),
                timeout_seconds=float(value["timeout_seconds"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RegistryError(f"canonical {field} contract is invalid") from exc

    @property
    def readiness_probe(self) -> ProbeSpec:
        value = self._probe("readiness", required=True)
        assert value is not None
        return value

    @property
    def warmup_probe(self) -> ProbeSpec | None:
        return self._probe("warmup", required=False)

    @property
    def activation_authentication(self) -> str:
        # v1 has no unauthenticated marker; absence therefore fails closed to bearer.
        return "bearer"

    @property
    def activation_mechanism(self) -> str:
        return self.gateway.scale_contract.activation_mode

    @property
    def model_revision(self) -> str:
        if self.variant_id is not None and self.variant_digest is not None:
            return f"variant:{self.variant_id}:{self.variant_digest}"
        if self.gateway.model_revision is None:
            raise RegistryError("routable model has no immutable revision")
        return self.gateway.model_revision

    def valid_at(self, when: datetime) -> bool:
        if self.lean_static:
            return True
        if self.variant_valid_until is None:
            return self.binding.valid_at(when)
        expires = datetime.fromisoformat(self.variant_valid_until[:-1] + "+00:00")
        return when.astimezone(UTC).replace(microsecond=0) <= expires

    @property
    def required_scopes(self) -> frozenset[str]:
        scopes: set[str] = set()
        if self.gateway.non_clinical:
            scopes.add("use.nonclinical")
        if self.gateway.commercial_use in {"prohibited", "blocked"}:
            scopes.add("use.noncommercial")
        return frozenset(scopes)

    @property
    def gpu_seconds_reservation(self) -> float:
        return float(self.gateway.gpu_allocation_count * self.max_gpu_seconds_per_attempt * self.max_attempts)


class Registry:
    """Stable facade over immutable canonical-loader snapshots.

    A refresh builds a complete candidate outside the critical section and then
    replaces one pointer. Any validation failure swaps the current snapshot to
    a route-free projection; callers can never observe a partially refreshed
    route set.
    """

    @dataclass(frozen=True)
    class _Source:
        catalog_dir: Path
        bindings_file: Path
        variant_promotions_file: Path | None
        lean_routes_file: Path | None
        repo_root: Path | None
        evidence_root: Path | None
        max_attempts: int
        max_gpu_seconds_per_attempt: float
        retry_base_seconds: float
        trusted_attestors_loader: Callable[[], Mapping[str, str] | None]
        fixed_validation_time: datetime | None

    @dataclass(frozen=True)
    class _Snapshot:
        catalog: GatewayCatalog
        models: Mapping[str, OperationalModel]

    def __init__(
        self,
        catalog: GatewayCatalog,
        models: dict[str, OperationalModel],
        *,
        source: _Source | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._snapshot = self._Snapshot(catalog, MappingProxyType(dict(sorted(models.items()))))
        self._source = source
        self._generation = 1
        self._checked_at = source.fixed_validation_time if source is not None else None
        self._healthy = True

    @property
    def catalog(self) -> GatewayCatalog:
        return self._current().catalog

    @staticmethod
    def _models_from_gateway(
        gateway: GatewayCatalog,
        *,
        max_attempts: int,
        max_gpu_seconds_per_attempt: float,
        retry_base_seconds: float,
    ) -> dict[str, OperationalModel]:
        models = {
            model_id: OperationalModel(
                gateway=model,
                max_attempts=max_attempts,
                max_gpu_seconds_per_attempt=max_gpu_seconds_per_attempt,
                retry_base_seconds=retry_base_seconds,
            )
            for model_id, model in gateway.models.items()
        }
        binding_count = sum(model.binding is not None for model in gateway.models.values())
        if binding_count and not gateway.routable_model_ids():
            raise RegistryError("bindings claim routes but the canonical intersection is empty")
        for model in models.values():
            if model.enabled:
                _ = model.binding
                _ = model.readiness_probe
        return models

    @staticmethod
    def _bind_variant_routes(gateway: GatewayCatalog, variants: VariantGatewayCatalog) -> Registry._Snapshot:
        """Project only the canonical signed variant/base/binding intersection.

        Static model variants are never consulted here. The models-lane loader
        has already reopened every signed supply/runtime/semantic/cohort/review
        subject and joined it to the exact disabled normal binding. Multiple
        promoted variants for one public model are ambiguous and fail closed.
        """

        routed: dict[str, OperationalModel] = {}
        gateway_models = dict(gateway.models)
        for variant_id, variant in variants.models.items():
            model_id = variant.promotion.exposed_model_id
            if model_id in routed:
                raise RegistryError("multiple model variants claim one public route")
            try:
                base = gateway_models[model_id]
            except KeyError as exc:  # pragma: no cover - canonical loader also enforces this join
                raise RegistryError("model variant has no canonical public model") from exc
            if base.routable or base.binding is None or base.binding.enabled:
                raise RegistryError("model variant route bypassed its disabled normal binding")
            projected = replace(
                base,
                routable=True,
                mcp_invocable=False,
                runtime_image_digest=variant.promotion.runtime_image_digest,
                binding=variant.promotion.binding,
            )
            gateway_models[model_id] = projected
            routed[model_id] = OperationalModel(
                gateway=projected,
                max_attempts=0,
                max_gpu_seconds_per_attempt=0,
                retry_base_seconds=0,
                variant_id=variant_id,
                variant_digest=variant.promotion.variant_digest,
                variant_valid_until=variant.promotion.valid_until,
            )
        projected_catalog = replace(gateway, models=MappingProxyType(dict(sorted(gateway_models.items()))))
        return Registry._Snapshot(projected_catalog, MappingProxyType(routed))

    @classmethod
    def _load_gateway(
        cls,
        *,
        catalog_dir: Path,
        bindings_file: Path,
        variant_promotions_file: Path | None,
        lean_routes_file: Path | None,
        repo_root: Path | None,
        evidence_root: Path | None,
        trusted_attestors: Mapping[str, str] | None,
        validation_time: datetime | None,
        max_attempts: int,
        max_gpu_seconds_per_attempt: float,
        retry_base_seconds: float,
    ) -> tuple[GatewayCatalog, dict[str, OperationalModel]]:
        catalog = load_catalog(catalog_dir, repo_root=repo_root)
        bindings = load_serving_bindings(
            bindings_file,
            catalog,
            evidence_root=evidence_root,
            trusted_attestors=trusted_attestors,
            validation_time=validation_time,
        )
        gateway = bind_gateway_catalog(catalog, bindings)
        variant_models: Mapping[str, OperationalModel] = {}
        if variant_promotions_file is not None:
            variants = load_variant_gateway_catalog(
                catalog,
                bindings,
                variant_promotions_file,
                evidence_root=evidence_root,
                trusted_attestors=trusted_attestors,
                validation_time=validation_time,
            )
            variant_snapshot = cls._bind_variant_routes(gateway, variants)
            gateway = variant_snapshot.catalog
            variant_models = variant_snapshot.models
        lean_model_ids: frozenset[str] = frozenset()
        if lean_routes_file is not None:
            gateway, lean_model_ids = bind_lean_routes(gateway, lean_routes_file, catalog=catalog)
        models = cls._models_from_gateway(
            gateway,
            max_attempts=max_attempts,
            max_gpu_seconds_per_attempt=max_gpu_seconds_per_attempt,
            retry_base_seconds=retry_base_seconds,
        )
        for model_id, variant in variant_models.items():
            models[model_id] = replace(
                variant,
                max_attempts=max_attempts,
                max_gpu_seconds_per_attempt=max_gpu_seconds_per_attempt,
                retry_base_seconds=retry_base_seconds,
            )
        for model_id in lean_model_ids:
            models[model_id] = replace(models[model_id], lean_static=True)
        return gateway, models

    @classmethod
    def load(
        cls,
        catalog_dir: Path,
        bindings_file: Path,
        *,
        repo_root: Path | None,
        evidence_root: Path | None,
        variant_promotions_file: Path | None = None,
        lean_routes_file: Path | None = None,
        max_attempts: int,
        max_gpu_seconds_per_attempt: float,
        retry_base_seconds: float,
        trusted_attestors: Mapping[str, str] | None = None,
        trusted_attestors_loader: Callable[[], Mapping[str, str] | None] | None = None,
        validation_time: datetime | None = None,
    ) -> Registry:
        if trusted_attestors is not None and trusted_attestors_loader is not None:
            raise RegistryError("configure either a static or reloadable route-attestor set")
        if trusted_attestors_loader is None:
            static_attestors = None if trusted_attestors is None else MappingProxyType(dict(trusted_attestors))

            def trusted_attestors_loader() -> Mapping[str, str] | None:
                return static_attestors

        source = cls._Source(
            catalog_dir=Path(catalog_dir),
            bindings_file=Path(bindings_file),
            variant_promotions_file=None if variant_promotions_file is None else Path(variant_promotions_file),
            lean_routes_file=None if lean_routes_file is None else Path(lean_routes_file),
            repo_root=repo_root,
            evidence_root=evidence_root,
            max_attempts=max_attempts,
            max_gpu_seconds_per_attempt=max_gpu_seconds_per_attempt,
            retry_base_seconds=retry_base_seconds,
            trusted_attestors_loader=trusted_attestors_loader,
            fixed_validation_time=validation_time,
        )
        try:
            gateway, models = cls._load_gateway(
                catalog_dir=Path(catalog_dir),
                bindings_file=Path(bindings_file),
                variant_promotions_file=source.variant_promotions_file,
                lean_routes_file=source.lean_routes_file,
                repo_root=repo_root,
                evidence_root=evidence_root,
                trusted_attestors=trusted_attestors_loader(),
                validation_time=validation_time,
                max_attempts=max_attempts,
                max_gpu_seconds_per_attempt=max_gpu_seconds_per_attempt,
                retry_base_seconds=retry_base_seconds,
            )
        except RegistryError:
            raise
        except (CatalogError, LeanRouteError, OSError, ValueError) as exc:
            raise RegistryError("canonical gateway catalog validation failed") from exc
        return cls(gateway, models, source=source)

    @staticmethod
    def _without_routes(snapshot: _Snapshot) -> _Snapshot:
        models = {
            model_id: replace(
                model,
                gateway=replace(model.gateway, routable=False, mcp_invocable=False),
            )
            for model_id, model in snapshot.models.items()
        }
        catalog = replace(
            snapshot.catalog,
            models=MappingProxyType({model_id: models[model_id].gateway for model_id in sorted(models)}),
        )
        return Registry._Snapshot(catalog, MappingProxyType(dict(sorted(models.items()))))

    def _withdraw(self, *, checked_at: datetime) -> None:
        with self._lock:
            self._snapshot = self._without_routes(self._snapshot)
            self._generation += 1
            self._checked_at = checked_at
            self._healthy = False

    def revalidate(self, *, validation_time: datetime | None = None) -> bool:
        """Reload trust, evidence, bindings, and typed catalog as one transaction."""

        source = self._source
        if source is None:
            return True
        checked_at = validation_time or source.fixed_validation_time or datetime.now(UTC)
        if checked_at.tzinfo is None:
            raise RegistryError("route revalidation time must be timezone-aware")
        try:
            gateway, models = self._load_gateway(
                catalog_dir=source.catalog_dir,
                bindings_file=source.bindings_file,
                variant_promotions_file=source.variant_promotions_file,
                lean_routes_file=source.lean_routes_file,
                repo_root=source.repo_root,
                evidence_root=source.evidence_root,
                trusted_attestors=source.trusted_attestors_loader(),
                validation_time=checked_at,
                max_attempts=source.max_attempts,
                max_gpu_seconds_per_attempt=source.max_gpu_seconds_per_attempt,
                retry_base_seconds=source.retry_base_seconds,
            )
            candidate = self._Snapshot(
                gateway,
                MappingProxyType(dict(sorted(models.items()))),
            )
        except (CatalogError, LeanRouteError, OSError, RegistryError, ValueError):
            self._withdraw(checked_at=checked_at)
            return False
        with self._lock:
            self._snapshot = candidate
            self._generation += 1
            self._checked_at = checked_at
            self._healthy = True
        return True

    def _current(self) -> _Snapshot:
        source = self._source
        with self._lock:
            snapshot = self._snapshot
            if source is None:
                return snapshot
            now = source.fixed_validation_time or datetime.now(UTC)
            if any(model.enabled and not model.valid_at(now) for model in snapshot.models.values()):
                self._snapshot = self._without_routes(snapshot)
                self._generation += 1
                self._checked_at = now
                self._healthy = False
                return self._snapshot
            return snapshot

    def validation_health(self) -> dict[str, object]:
        self._current()
        with self._lock:
            return {
                "healthy": self._healthy,
                "generation": self._generation,
                "checked_at": self._checked_at.isoformat() if self._checked_at is not None else None,
            }

    def get(self, model_id: str, *, require_enabled: bool = True) -> OperationalModel:
        try:
            model = self._current().models[model_id]
        except KeyError as exc:
            raise KeyError(f"unknown model: {model_id}") from exc
        if require_enabled and not model.enabled:
            raise RuntimeError("model is not routable")
        return model

    def list(self, *, enabled_only: bool = False) -> Sequence[OperationalModel]:
        return [model for model in self._current().models.values() if model.enabled or not enabled_only]

    def allowed(self, selectors: frozenset[str], *, enabled_only: bool = True) -> Sequence[OperationalModel]:
        wildcard = "*" in selectors
        return [
            model
            for model in self._current().models.values()
            if (wildcard or model.id in selectors) and (model.enabled or not enabled_only)
        ]

    def operation_for_protocol(self, model: OperationalModel, protocol: str) -> str:
        """Resolve the canonical protocol/operation relation without inventing policy."""

        if not model.enabled:
            raise RegistryError("model is not routable")
        binding = model.binding
        canonical_protocols = model.gateway.protocols
        canonical_operations = model.gateway.policy_operations
        if binding.model_id != model.id:
            raise RegistryError("serving binding belongs to a different model")
        if (
            model.gateway.protocols != canonical_protocols
            or binding.protocols != canonical_protocols
            or model.gateway.policy_operations != canonical_operations
            or binding.operations != canonical_operations
            or set(binding.endpoints) != set(canonical_protocols)
        ):
            raise RegistryError("serving binding differs from canonical protocol policy")
        if protocol not in canonical_protocols:
            raise RegistryError("model has no bound route for requested protocol")

        # The canonical catalog deliberately keeps transport protocols and
        # allowed semantic operations as separate exact sets. It does not
        # define positional pairing between two multi-item lists. A route can
        # therefore infer an operation only when the selected bound model has
        # exactly one policy operation; every other cardinality is fail-closed.
        operations = canonical_operations
        if not operations:
            raise RegistryError("bound protocol has no canonical policy operation")
        if len(operations) != 1:
            raise RegistryError("bound protocol policy operation is ambiguous")
        return operations[0]

    @staticmethod
    def authorize(model: OperationalModel, scopes: frozenset[str]) -> None:
        if model.required_scopes - scopes:
            raise PermissionError("token policy does not satisfy model use restrictions")

    def render_runtime_config(self) -> dict[str, Any]:
        return self.catalog.to_dict()
