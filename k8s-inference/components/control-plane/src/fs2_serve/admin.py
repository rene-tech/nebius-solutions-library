"""Read-only admin projections over bounded, injected data sources."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .admin_models import (
    AdminActivationPhase,
    AdminAutoscalingProjection,
    AdminCapabilityHealth,
    AdminCapacity,
    AdminCapacitySnapshot,
    AdminContext,
    AdminContextData,
    AdminContextOption,
    AdminEnvelope,
    AdminFleetCapacity,
    AdminHorizontalAutoscalerInventory,
    AdminKedaScaledObjectInventory,
    AdminKubernetesSnapshot,
    AdminKueueProjection,
    AdminLatency,
    AdminMeasurement,
    AdminMeta,
    AdminModelActivity,
    AdminModelDetail,
    AdminModelIdentity,
    AdminModelList,
    AdminModelMetrics,
    AdminModelPolicy,
    AdminModelRuntime,
    AdminModelState,
    AdminModelStateCount,
    AdminModelSummary,
    AdminNodePoolInventory,
    AdminNodeScalerProjection,
    AdminObservability,
    AdminObservabilityComponent,
    AdminObservabilityLaunch,
    AdminObservabilitySignals,
    AdminObservabilitySnapshot,
    AdminOperationDetail,
    AdminOperationItem,
    AdminOperationList,
    AdminOperationQuery,
    AdminOperationRecord,
    AdminOperationTiming,
    AdminOverview,
    AdminPrometheusModel,
    AdminPrometheusSnapshot,
    AdminQualificationSnapshot,
    AdminReconciliation,
    AdminRuntimeOrigin,
    AdminSource,
    AdminSourceState,
    AdminUsageRow,
    AdminUsageWindow,
    AdminValueState,
    AdminWarning,
)
from .models import OperationStatus
from .registry import OperationalModel, Registry
from .store import NotFoundError, Store

MAX_ADMIN_WINDOW = timedelta(days=31)
DEFAULT_ADMIN_WINDOW = timedelta(hours=1)
MAX_SOURCE_AGE_SECONDS = 90.0
MAX_CLOCK_SKEW_SECONDS = 300.0
DEFAULT_ADAPTER_TIMEOUT_SECONDS = 2.0
_MODEL_SELECTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_SUPPORTED_CATALOG_STATES = frozenset({"qualified", "lean-live-verified"})
_TOKEN_REPORTING_PROTOCOLS = frozenset({"openai-chat", "openai-completions", "openai-embeddings"})


class AdminProblemError(RuntimeError):
    """A stable operator-facing failure without reflected backend detail."""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


class AdminAdapterUnavailableError(RuntimeError):
    pass


class KubernetesAdminAdapter(Protocol):
    async def snapshot(self, model_ids: tuple[str, ...]) -> AdminKubernetesSnapshot: ...


class PrometheusAdminAdapter(Protocol):
    async def snapshot(
        self, model_ids: tuple[str, ...], *, from_at: datetime, to_at: datetime
    ) -> AdminPrometheusSnapshot: ...


class CapacityAdminAdapter(Protocol):
    async def snapshot(self) -> AdminCapacitySnapshot: ...


class ObservabilityAdminAdapter(Protocol):
    async def snapshot(
        self,
        *,
        context: AdminContext,
        model_id: str | None,
        operation_id: UUID | None,
    ) -> AdminObservabilitySnapshot: ...


class UnavailableKubernetesAdminAdapter:
    async def snapshot(self, model_ids: tuple[str, ...]) -> AdminKubernetesSnapshot:
        del model_ids
        raise AdminAdapterUnavailableError("kubernetes admin adapter is not configured")


class UnavailablePrometheusAdminAdapter:
    async def snapshot(
        self, model_ids: tuple[str, ...], *, from_at: datetime, to_at: datetime
    ) -> AdminPrometheusSnapshot:
        del model_ids, from_at, to_at
        raise AdminAdapterUnavailableError("prometheus admin adapter is not configured")


class UnavailableCapacityAdminAdapter:
    async def snapshot(self) -> AdminCapacitySnapshot:
        raise AdminAdapterUnavailableError("capacity admin adapter is not configured")


class UnavailableObservabilityAdminAdapter:
    async def snapshot(
        self,
        *,
        context: AdminContext,
        model_id: str | None,
        operation_id: UUID | None,
    ) -> AdminObservabilitySnapshot:
        del context, model_id, operation_id
        raise AdminAdapterUnavailableError("observability admin adapter is not configured")


class CachedKubernetesAdminAdapter:
    """Short-lived cache around an allow-listed typed adapter, never raw objects."""

    def __init__(
        self,
        delegate: KubernetesAdminAdapter,
        *,
        ttl_seconds: float = 15,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= ttl_seconds <= 60:
            raise ValueError("Kubernetes admin cache TTL is outside the bound")
        self.delegate = delegate
        self.ttl_seconds = ttl_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._cache: tuple[tuple[str, ...], datetime, AdminKubernetesSnapshot] | None = None

    async def snapshot(self, model_ids: tuple[str, ...]) -> AdminKubernetesSnapshot:
        now = self.clock().astimezone(UTC)
        cached = self._cache
        if cached is not None and cached[0] == model_ids and now < cached[1]:
            return cached[2].model_copy(deep=True)
        async with self._lock:
            now = self.clock().astimezone(UTC)
            cached = self._cache
            if cached is not None and cached[0] == model_ids and now < cached[1]:
                return cached[2].model_copy(deep=True)
            value = await self.delegate.snapshot(model_ids)
            self._cache = (model_ids, now + timedelta(seconds=self.ttl_seconds), value.model_copy(deep=True))
            return value


class PrometheusQueryTemplates:
    """Construct only fixed, bounded PromQL; never accept a browser query string."""

    @staticmethod
    def _selector(model_id: str | None) -> str:
        if model_id is None:
            return ""
        if _MODEL_SELECTOR.fullmatch(model_id) is None:
            raise ValueError("model selector is invalid")
        return f'model="{model_id}"'

    @classmethod
    def for_window(cls, *, model_id: str | None, seconds: int) -> Mapping[str, str]:
        if not 60 <= seconds <= int(MAX_ADMIN_WINDOW.total_seconds()):
            raise ValueError("Prometheus range is outside the bound")
        selector = cls._selector(model_id)
        labels = f"{{{selector}}}" if selector else ""
        window = f"{seconds}s"
        requests = f"sum(max by (model, protocol, outcome) (rate(fs2_serve_requests_total{labels}[{window}])))"
        terminal = f"sum(max by (model, protocol, outcome) (fs2_serve_requests_total{labels}))"
        error_labels = f'{{{selector + "," if selector else ""}outcome!="succeeded"}}'
        error_requests = (
            f"sum(max by (model, protocol, outcome) (rate(fs2_serve_requests_total{error_labels}[{window}])))"
        )
        errors = f"clamp((({error_requests}) or vector(0)) / clamp_min(({requests}), 1e-12), 0, 1)"
        latency = {
            quantile: (
                f"histogram_quantile({quantile}, sum by (le) "
                f"(rate(fs2_serve_request_duration_seconds_bucket{labels}[{window}])))"
            )
            for quantile in ("0.50", "0.95", "0.99")
        }
        return {
            "requests_per_second": requests,
            "terminal_operations": terminal,
            "error_rate": errors,
            "latency_p50_seconds": latency["0.50"],
            "latency_p95_seconds": latency["0.95"],
            "latency_p99_seconds": latency["0.99"],
        }

    @staticmethod
    def by_model_for_window(*, seconds: int) -> Mapping[str, str]:
        """Return six fixed vector queries, independent of catalog size."""

        if not 60 <= seconds <= int(MAX_ADMIN_WINDOW.total_seconds()):
            raise ValueError("Prometheus range is outside the bound")
        window = f"{seconds}s"
        requests = f"sum by (model) (max by (model, protocol, outcome) (rate(fs2_serve_requests_total[{window}])))"
        terminal = "sum by (model) (max by (model, protocol, outcome) (fs2_serve_requests_total))"
        error_requests = (
            "sum by (model) (max by (model, protocol, outcome) "
            f'(rate(fs2_serve_requests_total{{outcome!="succeeded"}}[{window}])))'
        )
        errors = f"clamp((({error_requests}) or on(model) (0 * ({requests}))) / clamp_min(({requests}), 1e-12), 0, 1)"
        latency = {
            quantile: (
                f"histogram_quantile({quantile}, sum by (model, le) "
                f"(rate(fs2_serve_request_duration_seconds_bucket[{window}])))"
            )
            for quantile in ("0.50", "0.95", "0.99")
        }
        return {
            "requests_per_second": requests,
            "terminal_operations": terminal,
            "error_rate": errors,
            "latency_p50_seconds": latency["0.50"],
            "latency_p95_seconds": latency["0.95"],
            "latency_p99_seconds": latency["0.99"],
        }


class AdminObservabilityQueryTemplates:
    """Fixed aggregate queries; callers cannot supply labels or expressions."""

    @staticmethod
    def for_window(seconds: int) -> Mapping[str, str]:
        if not 60 <= seconds <= int(MAX_ADMIN_WINDOW.total_seconds()):
            raise ValueError("Prometheus range is outside the bound")
        window = f"{seconds}s"
        return {
            "gpu_utilization_ratio": "avg(DCGM_FI_DEV_GPU_UTIL) / 100",
            "gpu_memory_utilization_ratio": (
                "sum(DCGM_FI_DEV_FB_USED) / clamp_min(sum(DCGM_FI_DEV_FB_USED) + sum(DCGM_FI_DEV_FB_FREE), 1)"
            ),
            "otel_refused_items_per_second": (
                f"(sum(rate(otelcol_receiver_refused_spans[{window}])) or vector(0)) + "
                f"(sum(rate(otelcol_receiver_refused_log_records[{window}])) or vector(0)) + "
                f"(sum(rate(otelcol_receiver_refused_metric_points[{window}])) or vector(0))"
            ),
            "otel_export_failures_per_second": (
                f"(sum(rate(otelcol_exporter_send_failed_spans[{window}])) or vector(0)) + "
                f"(sum(rate(otelcol_exporter_send_failed_log_records[{window}])) or vector(0)) + "
                f"(sum(rate(otelcol_exporter_send_failed_metric_points[{window}])) or vector(0))"
            ),
        }


@dataclass(frozen=True)
class AdminContextConfig:
    options: tuple[AdminContextOption, ...] = ()
    default_index: int = 0

    def __post_init__(self) -> None:
        identities = {(item.project, item.cluster, item.region) for item in self.options}
        if len(identities) != len(self.options):
            raise ValueError("admin context options contain a duplicate identity")
        if self.options and not 0 <= self.default_index < len(self.options):
            raise ValueError("admin default context is outside the options")


@dataclass(frozen=True)
class _DatabaseSnapshot:
    activity: Mapping[str, AdminModelActivity]
    usage: AdminUsageWindow
    queue_ages: Mapping[str, float]
    observed_at: datetime


def derive_model_state(
    *,
    catalog_supported: bool,
    sources_fresh: bool,
    health_failure: bool,
    activation_phase: AdminActivationPhase | str,
    desired_replicas: int,
    ready_replicas: int,
    queued_operations: int,
) -> tuple[AdminModelState, str]:
    """Apply the sealed seven-state precedence exactly once."""

    phase = AdminActivationPhase(activation_phase)
    if not catalog_supported:
        return AdminModelState.UNSUPPORTED, "catalog compatibility is not qualified"
    if not sources_fresh:
        return AdminModelState.UNKNOWN, "a required observed-state source is unavailable or stale"
    if health_failure or phase == AdminActivationPhase.FAILED:
        return AdminModelState.UNHEALTHY, "workload or activation health failed"
    if phase == AdminActivationPhase.CLAIMED or desired_replicas > ready_replicas:
        return AdminModelState.LOADING, "activation is claimed or desired replicas are not ready"
    if ready_replicas > 0:
        return AdminModelState.HOT, "at least one healthy Service-selected replica is Ready"
    if phase == AdminActivationPhase.QUEUED or queued_operations > 0:
        return AdminModelState.QUEUED, "durable demand is waiting without a ready replica"
    return AdminModelState.COLD, "qualified model is idle at zero ready replicas"


def catalog_compatibility_supported(model: OperationalModel) -> bool:
    """Recognize only canonical or retained-route validated support states."""

    return model.gateway.support_state in _SUPPORTED_CATALOG_STATES


def derive_operational_model_state(
    model: OperationalModel,
    *,
    sources_fresh: bool,
    health_failure: bool,
    activation_phase: AdminActivationPhase | str,
    desired_replicas: int,
    ready_replicas: int,
    queued_operations: int,
) -> tuple[AdminModelState, str]:
    return derive_model_state(
        catalog_supported=catalog_compatibility_supported(model),
        sources_fresh=sources_fresh,
        health_failure=health_failure,
        activation_phase=activation_phase,
        desired_replicas=desired_replicas,
        ready_replicas=ready_replicas,
        queued_operations=queued_operations,
    )


def _available(value: float, unit: str, source: str) -> AdminMeasurement:
    return AdminMeasurement(value=value, unit=unit, state=AdminValueState.AVAILABLE, source=source)


def _estimated(value: float, unit: str, source: str) -> AdminMeasurement:
    return AdminMeasurement(
        value=value,
        unit=unit,
        state=AdminValueState.ESTIMATED,
        source=source,
        reason="value is accounting estimate, not measured device utilization",
    )


def _unavailable(unit: str, source: str, reason: str) -> AdminMeasurement:
    return AdminMeasurement(value=None, unit=unit, state=AdminValueState.UNAVAILABLE, source=source, reason=reason)


def _unavailable_capacity(state: AdminSourceState, reason: str) -> AdminCapacity:
    return AdminCapacity(
        node_pools=AdminNodePoolInventory(state=state, reason=reason, items=[]),
        kueue=AdminKueueProjection(
            state=state,
            reason=reason,
            resource_flavors=[],
            cluster_queues=[],
            local_queues=[],
            cohorts=[],
            cohorts_state=state,
            cohorts_reason=reason,
            workloads=[],
        ),
        autoscaling=AdminAutoscalingProjection(
            hpa=AdminHorizontalAutoscalerInventory(
                state=state,
                reason=reason,
                horizontal_pod_autoscalers=[],
            ),
            keda=AdminKedaScaledObjectInventory(
                state=state,
                reason=reason,
                keda_scaled_objects=[],
            ),
        ),
        node_scaler=AdminNodeScalerProjection(
            state=state,
            configured=None,
            healthy=None,
            reason=reason,
        ),
    )


_OBSERVABILITY_COMPONENTS = (
    ("grafana", "Grafana"),
    ("prometheus", "Prometheus"),
    ("loki", "Loki"),
    ("otel", "OpenTelemetry Collector"),
    ("dcgm", "NVIDIA DCGM"),
    ("kueue", "Kueue"),
    ("keda", "KEDA"),
    ("alertmanager", "Alertmanager"),
    ("tempo", "Tempo"),
)


def _unavailable_observability(reason: str) -> AdminObservability:
    return AdminObservability(
        components=[
            AdminObservabilityComponent(
                id=component_id,
                display_name=display_name,
                installed=None,
                health=AdminCapabilityHealth.UNKNOWN,
                data_present=None,
                launch=AdminObservabilityLaunch(enabled=False, reason=reason),
                reason=reason,
            )
            for component_id, display_name in _OBSERVABILITY_COMPONENTS
        ],
        signals=AdminObservabilitySignals(
            gpu_utilization_ratio=_unavailable("ratio", "dcgm", reason),
            gpu_memory_utilization_ratio=_unavailable("ratio", "dcgm", reason),
            otel_refused_items_per_second=_unavailable("items/second", "prometheus", reason),
            otel_export_failures_per_second=_unavailable("items/second", "prometheus", reason),
        ),
    )


class AdminReadService:
    def __init__(
        self,
        *,
        registry: Registry,
        store: Store,
        kubernetes: KubernetesAdminAdapter | None = None,
        prometheus: PrometheusAdminAdapter | None = None,
        capacity: CapacityAdminAdapter | None = None,
        observability: ObservabilityAdminAdapter | None = None,
        contexts: AdminContextConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        source_max_age_seconds: float = MAX_SOURCE_AGE_SECONDS,
        adapter_timeout_seconds: float = DEFAULT_ADAPTER_TIMEOUT_SECONDS,
    ) -> None:
        if not 1 <= source_max_age_seconds <= 3600:
            raise ValueError("admin source freshness bound is invalid")
        if not 0.1 <= adapter_timeout_seconds <= 10:
            raise ValueError("admin adapter timeout is outside the bound")
        self.registry = registry
        self.store = store
        self.kubernetes = kubernetes or UnavailableKubernetesAdminAdapter()
        self.prometheus = prometheus or UnavailablePrometheusAdminAdapter()
        self.capacity_adapter = capacity or UnavailableCapacityAdminAdapter()
        self.observability_adapter = observability or UnavailableObservabilityAdminAdapter()
        self.contexts = contexts or AdminContextConfig()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.source_max_age_seconds = source_max_age_seconds
        self.adapter_timeout_seconds = adapter_timeout_seconds

    def resolve_context(
        self,
        *,
        project: str | None,
        cluster: str | None,
        region: str | None,
        from_at: datetime | None,
        to_at: datetime | None,
        timezone: str,
    ) -> AdminContext:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("admin clock must be timezone-aware")
        if not 1 <= len(timezone) <= 64:
            raise AdminProblemError(400, "invalid_timezone", "timezone is outside the accepted bound")
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise AdminProblemError(400, "invalid_timezone", "timezone is not recognized") from None
        end = to_at or now
        start = from_at or (end - DEFAULT_ADMIN_WINDOW)
        if start.tzinfo is None or start.utcoffset() is None or end.tzinfo is None or end.utcoffset() is None:
            raise AdminProblemError(400, "invalid_time_range", "time bounds must include an offset")
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        if start >= end or end - start > MAX_ADMIN_WINDOW or end > now.astimezone(UTC) + timedelta(seconds=300):
            raise AdminProblemError(400, "invalid_time_range", "time range is outside the accepted bound")

        selectors = (project, cluster, region)
        option: AdminContextOption | None = None
        if self.contexts.options:
            if all(value is None for value in selectors):
                option = self.contexts.options[self.contexts.default_index]
            else:
                matches = [
                    item
                    for item in self.contexts.options
                    if (project is None or item.project == project)
                    and (cluster is None or item.cluster == cluster)
                    and (region is None or item.region == region)
                ]
                if len(matches) != 1:
                    raise AdminProblemError(
                        400,
                        "invalid_context",
                        "project, cluster, and region do not select one server-authorized context",
                    )
                option = matches[0]
        elif any(value is not None for value in selectors):
            raise AdminProblemError(400, "invalid_context", "no server-authorized cluster context is configured")
        return AdminContext(
            project=option.project if option else None,
            cluster=option.cluster if option else None,
            region=option.region if option else None,
            from_at=start,
            to_at=end,
            timezone=timezone,
        )

    def context(self, context: AdminContext) -> AdminEnvelope[AdminContextData]:
        now = self.clock().astimezone(UTC)
        source = AdminSource(
            id="context",
            state=AdminSourceState.AVAILABLE if self.contexts.options else AdminSourceState.UNAVAILABLE,
            observed_at=now,
            age_seconds=0,
            reason=None if self.contexts.options else "no server-authorized context is configured",
        )
        return AdminEnvelope(
            meta=AdminMeta(generated_at=now, context=context, sources=[source]),
            data=AdminContextData(selected=context, options=list(self.contexts.options)),
        )

    def _fresh(self, observed_at: datetime, now: datetime) -> tuple[bool, float]:
        age = (now - observed_at.astimezone(UTC)).total_seconds()
        return -MAX_CLOCK_SKEW_SECONDS <= age <= self.source_max_age_seconds, max(0.0, age)

    async def _database_snapshot(self, model_ids: tuple[str, ...], context: AdminContext) -> _DatabaseSnapshot:
        activity, usage, queue_ages = await asyncio.gather(
            self.store.admin_model_activity(model_ids),
            self.store.admin_usage_window(from_at=context.from_at, to_at=context.to_at),
            self.store.oldest_queue_age(),
        )
        return _DatabaseSnapshot(
            activity={item.model_id: item for item in activity},
            usage=usage,
            queue_ages=queue_ages,
            observed_at=self.clock().astimezone(UTC),
        )

    async def _source_snapshots(
        self, models: Sequence[OperationalModel], context: AdminContext
    ) -> tuple[
        _DatabaseSnapshot | None,
        AdminKubernetesSnapshot | None,
        AdminPrometheusSnapshot | None,
        list[AdminSource],
        list[AdminWarning],
    ]:
        now = self.clock().astimezone(UTC)
        model_ids = tuple(model.id for model in models)
        results = await asyncio.gather(
            asyncio.wait_for(
                self._database_snapshot(model_ids, context),
                timeout=self.adapter_timeout_seconds,
            ),
            asyncio.wait_for(
                self.kubernetes.snapshot(model_ids),
                timeout=self.adapter_timeout_seconds,
            ),
            asyncio.wait_for(
                self.prometheus.snapshot(model_ids, from_at=context.from_at, to_at=context.to_at),
                timeout=self.adapter_timeout_seconds,
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
        database = results[0] if isinstance(results[0], _DatabaseSnapshot) else None
        kubernetes = results[1] if isinstance(results[1], AdminKubernetesSnapshot) else None
        prometheus = results[2] if isinstance(results[2], AdminPrometheusSnapshot) else None
        health = self.registry.validation_health()
        checked = health.get("checked_at")
        catalog_observed = datetime.fromisoformat(str(checked)) if checked is not None else now
        catalog_fresh = bool(health.get("healthy"))
        sources = [
            AdminSource(
                id="catalog",
                state=AdminSourceState.AVAILABLE if catalog_fresh else AdminSourceState.UNAVAILABLE,
                observed_at=catalog_observed,
                age_seconds=max(0.0, (now - catalog_observed.astimezone(UTC)).total_seconds()),
                reason=None if catalog_fresh else "catalog route validation is unhealthy",
            )
        ]
        warnings: list[AdminWarning] = []
        for source_id, value in (
            ("postgresql", database),
            ("kubernetes", kubernetes),
            ("prometheus", prometheus),
        ):
            observed = value.observed_at if value is not None else None
            fresh, age = self._fresh(observed, now) if observed is not None else (False, None)
            state = (
                AdminSourceState.AVAILABLE
                if fresh
                else (AdminSourceState.STALE if value is not None else AdminSourceState.UNAVAILABLE)
            )
            reason = None
            if state == AdminSourceState.STALE:
                reason = "source observation exceeded the freshness bound"
            elif state == AdminSourceState.UNAVAILABLE:
                reason = "source adapter or query is unavailable"
            sources.append(AdminSource(id=source_id, state=state, observed_at=observed, age_seconds=age, reason=reason))
            if state != AdminSourceState.AVAILABLE:
                warnings.append(
                    AdminWarning(
                        source=source_id,
                        code="partial_source_unavailable" if value is None else "partial_source_stale",
                        message=f"{source_id} data is {state.value}; affected values are unavailable",
                    )
                )
        return database, kubernetes, prometheus, sources, warnings

    @staticmethod
    def _identity(model: OperationalModel) -> AdminModelIdentity:
        gateway = model.gateway
        binding = gateway.binding
        endpoints = dict(gateway.endpoints) if binding is not None else {}
        projection = gateway.qualification
        active_runtime = None if projection is None else AdminRuntimeOrigin.model_validate(projection["runtime_origin"])
        qualification = (
            None
            if projection is None
            else AdminQualificationSnapshot.model_validate(
                {
                    "kind": "reviewed-evidence-snapshot",
                    "authority": projection["qualification_authority"],
                    "observed_at": projection["observed_at"],
                    "states": projection["states"],
                }
            )
        )
        return AdminModelIdentity(
            id=model.id,
            display_name=gateway.display_name,
            family=gateway.family,
            support_state=gateway.support_state,
            enabled=model.enabled,
            model_revision=gateway.model_revision,
            runtime_kind=gateway.runtime_kind,
            runtime_image_digest=gateway.runtime_image_digest,
            gpu_class=gateway.gpu_class,
            gpu_count=gateway.gpu_allocation_count,
            execution_mode=gateway.execution_mode,
            protocols=sorted(gateway.protocols),
            public_endpoints=dict(sorted(endpoints.items())),
            mcp_exposed=bool(binding is not None and binding.mcp_enabled and gateway.mcp_invocable),
            mcp_tool_name=binding.mcp_tool_name if binding is not None and binding.mcp_enabled else None,
            active_runtime=active_runtime,
            qualification=qualification,
            policy=AdminModelPolicy(
                license_id=gateway.license_id,
                non_clinical=gateway.non_clinical,
                commercial_use=gateway.commercial_use,
            ),
        )

    @staticmethod
    def _token_rate(
        *,
        source_ready: bool,
        token_reporting_configured: bool,
        terminal_operations: int,
        token_reported_operations: int,
        input_tokens: int,
        output_tokens: int,
        window_seconds: float,
    ) -> AdminMeasurement:
        if not source_ready:
            return _unavailable("tokens/second", "postgresql", "durable usage query is unavailable")
        if not token_reporting_configured:
            return _unavailable(
                "tokens/second",
                "postgresql",
                "the runtime does not use a token-reporting protocol",
            )
        if terminal_operations == 0:
            return _available(0.0, "tokens/second", "postgresql")
        if token_reported_operations == 0:
            return _unavailable(
                "tokens/second",
                "postgresql",
                "runtime token reporting is unavailable for the selected operations",
            )
        rate = (input_tokens + output_tokens) / window_seconds
        if token_reported_operations < terminal_operations:
            return AdminMeasurement(
                value=rate,
                unit="tokens/second",
                state=AdminValueState.ESTIMATED,
                source="postgresql",
                reason=(
                    f"{token_reported_operations} of {terminal_operations} terminal operations reported token usage"
                ),
            )
        return _available(rate, "tokens/second", "postgresql")

    @staticmethod
    def _latency(
        prometheus: AdminPrometheusModel | AdminPrometheusSnapshot | None,
        durable: AdminUsageRow | AdminUsageWindow | None,
    ) -> AdminLatency:
        def metric(field: str) -> AdminMeasurement:
            durable_value = getattr(durable, field, None) if durable is not None else None
            if durable_value is not None:
                return _available(float(durable_value), "seconds", "postgresql")
            prometheus_value = getattr(prometheus, field, None) if prometheus is not None else None
            if prometheus_value is not None:
                return _available(float(prometheus_value), "seconds", "prometheus")
            return _unavailable(
                "seconds",
                "postgresql",
                "no terminal operation latency is available in the selected window",
            )

        return AdminLatency(
            p50_seconds=metric("latency_p50_seconds"),
            p95_seconds=metric("latency_p95_seconds"),
            p99_seconds=metric("latency_p99_seconds"),
            ttft_p95_seconds=_unavailable("seconds", "postgresql", "TTFT is not instrumented"),
        )

    def _model_summary(
        self,
        model: OperationalModel,
        *,
        database: _DatabaseSnapshot | None,
        kubernetes: AdminKubernetesSnapshot | None,
        prometheus: AdminPrometheusSnapshot | None,
        sources: Sequence[AdminSource],
    ) -> AdminModelSummary:
        source_states = {source.id: source.state for source in sources}
        activity = database.activity.get(model.id) if database is not None else None
        observed = next((item for item in kubernetes.models if item.model_id == model.id), None) if kubernetes else None
        usage_by_model = {row.model_id: row for row in database.usage.rows} if database else {}
        usage = usage_by_model.get(model.id)
        prom = next((item for item in prometheus.models if item.model_id == model.id), None) if prometheus else None
        required_fresh = (
            source_states.get("catalog") == AdminSourceState.AVAILABLE
            and source_states.get("postgresql") == AdminSourceState.AVAILABLE
            and source_states.get("kubernetes") == AdminSourceState.AVAILABLE
            and activity is not None
            and observed is not None
            and observed.semantic_healthy is not None
        )
        phase = activity.activation_phase if activity is not None else AdminActivationPhase.NONE
        queued = activity.queued_operations if activity is not None else 0
        desired = observed.desired_replicas if observed is not None else 0
        ready = observed.ready_replicas if observed is not None else 0
        state, reason = derive_operational_model_state(
            model,
            sources_fresh=required_fresh,
            health_failure=observed.semantic_healthy is False if observed is not None else False,
            activation_phase=phase,
            desired_replicas=desired,
            ready_replicas=ready,
            queued_operations=queued,
        )
        database_ready = source_states.get("postgresql") == AdminSourceState.AVAILABLE
        prom_ready = source_states.get("prometheus") == AdminSourceState.AVAILABLE and prom is not None
        operations = usage.terminal_operations if usage is not None else 0
        errors = usage.error_operations if usage is not None else 0
        error_rate = (errors / operations) if operations else 0.0
        return AdminModelSummary(
            identity=self._identity(model),
            runtime=AdminModelRuntime(
                state=state,
                reason=reason,
                activation_phase=phase if activity is not None else None,
                desired_replicas=observed.desired_replicas if observed is not None else None,
                ready_replicas=observed.ready_replicas if observed is not None else None,
                queued_operations=activity.queued_operations if activity is not None else None,
                semantic_healthy=observed.semantic_healthy if observed is not None else None,
                observed_at=kubernetes.observed_at if observed is not None and kubernetes is not None else None,
            ),
            metrics=AdminModelMetrics(
                terminal_operations=(
                    _available(float(operations), "operations", "postgresql")
                    if database_ready
                    else _unavailable("operations", "postgresql", "durable usage query is unavailable")
                ),
                requests_per_second=(
                    _available(float(prom.requests_per_second), "requests/second", "prometheus")
                    if prom_ready and prom is not None and prom.requests_per_second is not None
                    else _unavailable("requests/second", "prometheus", "request-rate series is unavailable")
                ),
                error_operations=(
                    _available(float(errors), "operations", "postgresql")
                    if database_ready
                    else _unavailable("operations", "postgresql", "durable usage query is unavailable")
                ),
                error_rate=(
                    _available(error_rate, "ratio", "postgresql")
                    if database_ready
                    else _unavailable("ratio", "postgresql", "durable usage query is unavailable")
                ),
                estimated_gpu_seconds=(
                    _estimated(usage.estimated_gpu_seconds if usage is not None else 0.0, "gpu-seconds", "postgresql")
                    if database_ready
                    else _unavailable("gpu-seconds", "postgresql", "durable usage query is unavailable")
                ),
                measured_gpu_seconds=_unavailable(
                    "gpu-seconds",
                    "dcgm",
                    "per-model time-integrated DCGM GPU-seconds are not instrumented",
                ),
                tokens_per_second=self._token_rate(
                    source_ready=database_ready,
                    token_reporting_configured=bool(_TOKEN_REPORTING_PROTOCOLS.intersection(model.gateway.protocols)),
                    terminal_operations=operations,
                    token_reported_operations=usage.token_reported_operations if usage is not None else 0,
                    input_tokens=usage.input_tokens if usage is not None else 0,
                    output_tokens=usage.output_tokens if usage is not None else 0,
                    window_seconds=(database.usage.to_at - database.usage.from_at).total_seconds()
                    if database is not None
                    else 1,
                ),
                latency=self._latency(prom if prom_ready else None, usage if database_ready else None),
                cold_start_seconds=(
                    _available(usage.cold_start_seconds if usage is not None else 0.0, "seconds", "postgresql")
                    if database_ready
                    else _unavailable("seconds", "postgresql", "cold-start accounting is unavailable")
                ),
            ),
        )

    async def model_list(
        self,
        context: AdminContext,
        *,
        search: str | None = None,
        state: AdminModelState | None = None,
        limit: int = 200,
    ) -> AdminEnvelope[AdminModelList]:
        models = list(self.registry.list())
        database, kubernetes, prometheus, sources, warnings = await self._source_snapshots(models, context)
        items = [
            self._model_summary(
                model,
                database=database,
                kubernetes=kubernetes,
                prometheus=prometheus,
                sources=sources,
            )
            for model in models
        ]
        if search:
            needle = search.casefold()
            items = [
                item
                for item in items
                if needle in item.identity.id.casefold() or needle in item.identity.display_name.casefold()
            ]
        if state is not None:
            items = [item for item in items if item.runtime.state == state]
        items.sort(key=lambda item: item.identity.id)
        total = len(items)
        now = self.clock().astimezone(UTC)
        return AdminEnvelope(
            meta=AdminMeta(generated_at=now, context=context, sources=sources, warnings=warnings),
            data=AdminModelList(items=items[:limit], total=total),
        )

    async def model_detail(self, context: AdminContext, model_id: str) -> AdminEnvelope[AdminModelDetail]:
        try:
            model = self.registry.get(model_id, require_enabled=False)
        except KeyError:
            raise AdminProblemError(404, "model_not_found", "model was not found") from None
        database, kubernetes, prometheus, sources, warnings = await self._source_snapshots([model], context)
        summary = self._model_summary(
            model,
            database=database,
            kubernetes=kubernetes,
            prometheus=prometheus,
            sources=sources,
        )
        now = self.clock().astimezone(UTC)
        return AdminEnvelope(
            meta=AdminMeta(generated_at=now, context=context, sources=sources, warnings=warnings),
            data=AdminModelDetail(
                model=summary,
                snapshot_restore_seconds=_unavailable(
                    "seconds", "postgresql", "snapshot restore timing is not recorded in the operation schema"
                ),
                cache_residency_bytes=_unavailable(
                    "bytes", "kubernetes", "cache residency is not exposed by the observed-state adapter"
                ),
                cold_start_phase_breakdown=_unavailable(
                    "seconds", "postgresql", "cold-start phase timestamps are not instrumented"
                ),
            ),
        )

    async def overview(self, context: AdminContext) -> AdminEnvelope[AdminOverview]:
        models = list(self.registry.list())
        database, kubernetes, prometheus, sources, warnings = await self._source_snapshots(models, context)
        source_states = {source.id: source.state for source in sources}
        items = [
            self._model_summary(
                model,
                database=database,
                kubernetes=kubernetes,
                prometheus=prometheus,
                sources=sources,
            )
            for model in models
        ]
        db_ready = source_states.get("postgresql") == AdminSourceState.AVAILABLE and database is not None
        prom_ready = source_states.get("prometheus") == AdminSourceState.AVAILABLE and prometheus is not None
        k8s_ready = source_states.get("kubernetes") == AdminSourceState.AVAILABLE and kubernetes is not None
        usage_rows = database.usage.rows if db_ready and database is not None else []
        token_model_ids = {
            model.id for model in models if _TOKEN_REPORTING_PROTOCOLS.intersection(model.gateway.protocols)
        }
        token_usage_rows = [row for row in usage_rows if row.model_id in token_model_ids]
        token_terminal = sum(row.terminal_operations for row in token_usage_rows)
        token_reported = sum(row.token_reported_operations for row in token_usage_rows)
        input_tokens = sum(row.input_tokens for row in token_usage_rows)
        output_tokens = sum(row.output_tokens for row in token_usage_rows)
        terminal = sum(row.terminal_operations for row in usage_rows)
        errors = sum(row.error_operations for row in usage_rows)
        estimated_gpu = sum(row.estimated_gpu_seconds for row in usage_rows)
        queue = sum(
            int(item.runtime.queued_operations or 0) for item in items if item.runtime.queued_operations is not None
        )
        queue_age = max(database.queue_ages.values(), default=0.0) if db_ready and database is not None else 0.0
        states = [
            AdminModelStateCount(state=value, models=sum(item.runtime.state == value for item in items))
            for value in AdminModelState
        ]
        prom_count = float(prometheus.terminal_operations) if prom_ready and prometheus is not None else None
        difference = prom_count - terminal if prom_count is not None and db_ready else None
        now = self.clock().astimezone(UTC)
        return AdminEnvelope(
            meta=AdminMeta(generated_at=now, context=context, sources=sources, warnings=warnings),
            data=AdminOverview(
                model_states=states,
                requests_per_second=(
                    _available(prometheus.requests_per_second, "requests/second", "prometheus")
                    if prom_ready and prometheus is not None
                    else _unavailable("requests/second", "prometheus", "request-rate series is unavailable")
                ),
                tokens_per_second=self._token_rate(
                    source_ready=db_ready,
                    token_reporting_configured=bool(token_model_ids),
                    terminal_operations=token_terminal,
                    token_reported_operations=token_reported,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    window_seconds=(database.usage.to_at - database.usage.from_at).total_seconds()
                    if database is not None
                    else 1,
                ),
                terminal_operations=(
                    _available(float(terminal), "operations", "postgresql")
                    if db_ready
                    else _unavailable("operations", "postgresql", "durable usage query is unavailable")
                ),
                error_operations=(
                    _available(float(errors), "operations", "postgresql")
                    if db_ready
                    else _unavailable("operations", "postgresql", "durable usage query is unavailable")
                ),
                error_rate=(
                    _available(errors / terminal if terminal else 0.0, "ratio", "postgresql")
                    if db_ready
                    else _unavailable("ratio", "postgresql", "durable usage query is unavailable")
                ),
                estimated_gpu_seconds=(
                    _estimated(estimated_gpu, "gpu-seconds", "postgresql")
                    if db_ready
                    else _unavailable("gpu-seconds", "postgresql", "durable usage query is unavailable")
                ),
                measured_gpu_seconds=_unavailable(
                    "gpu-seconds",
                    "dcgm",
                    "time-integrated DCGM GPU-seconds are not instrumented",
                ),
                queued_operations=(
                    _available(float(queue), "operations", "postgresql")
                    if db_ready
                    else _unavailable("operations", "postgresql", "durable queue query is unavailable")
                ),
                oldest_queue_age_seconds=(
                    _available(queue_age, "seconds", "postgresql")
                    if db_ready
                    else _unavailable("seconds", "postgresql", "durable queue query is unavailable")
                ),
                latency=self._latency(
                    prometheus if prom_ready else None,
                    database.usage if db_ready and database is not None else None,
                ),
                capacity=AdminFleetCapacity(
                    allocatable_gpus=(
                        _available(float(kubernetes.allocatable_gpus), "gpus", "kubernetes")
                        if k8s_ready and kubernetes is not None
                        else _unavailable("gpus", "kubernetes", "GPU capacity is unavailable")
                    ),
                    ready_gpu_nodes=(
                        _available(float(kubernetes.ready_gpu_nodes), "nodes", "kubernetes")
                        if k8s_ready and kubernetes is not None
                        else _unavailable("nodes", "kubernetes", "GPU node state is unavailable")
                    ),
                    preemptible_gpu_nodes=(
                        _available(float(kubernetes.preemptible_gpu_nodes), "nodes", "kubernetes")
                        if k8s_ready and kubernetes is not None
                        else _unavailable("nodes", "kubernetes", "capacity type is unavailable")
                    ),
                    active_gpu_replicas=(
                        _available(float(kubernetes.active_gpu_replicas), "replicas", "kubernetes")
                        if k8s_ready and kubernetes is not None
                        else _unavailable("replicas", "kubernetes", "GPU replica state is unavailable")
                    ),
                ),
                reconciliation=AdminReconciliation(
                    durable_terminal_operations=(
                        _available(float(terminal), "operations", "postgresql")
                        if db_ready
                        else _unavailable("operations", "postgresql", "durable total is unavailable")
                    ),
                    prometheus_terminal_operations=(
                        _available(prom_count, "operations", "prometheus")
                        if prom_count is not None
                        else _unavailable("operations", "prometheus", "bounded metric total is unavailable")
                    ),
                    difference=(
                        _available(difference, "operations", "bff")
                        if difference is not None
                        else _unavailable("operations", "bff", "both totals are required for reconciliation")
                    ),
                ),
            ),
        )

    @staticmethod
    def _projection_source(
        source_id: str,
        state: AdminSourceState,
        observed_at: datetime | None,
        now: datetime,
        reason: str | None,
    ) -> AdminSource:
        age = max(0.0, (now - observed_at.astimezone(UTC)).total_seconds()) if observed_at is not None else None
        return AdminSource(
            id=source_id,
            state=state,
            observed_at=observed_at,
            age_seconds=age,
            reason=reason,
        )

    @staticmethod
    def _projection_warning(source: AdminSource) -> AdminWarning | None:
        if source.state == AdminSourceState.AVAILABLE:
            return None
        return AdminWarning(
            source=source.id,
            code="partial_source_stale" if source.state == AdminSourceState.STALE else "partial_source_unavailable",
            message=f"{source.id} data is {source.state.value}; affected values are unavailable",
        )

    async def capacity(self, context: AdminContext) -> AdminEnvelope[AdminCapacity]:
        now = self.clock().astimezone(UTC)
        try:
            snapshot = await asyncio.wait_for(
                self.capacity_adapter.snapshot(),
                timeout=self.adapter_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except (AdminAdapterUnavailableError, OSError, RuntimeError, TimeoutError, ValueError):
            reason = "capacity adapter or query is unavailable"
            source = self._projection_source("capacity", AdminSourceState.UNAVAILABLE, None, now, reason)
            warning = self._projection_warning(source)
            return AdminEnvelope(
                meta=AdminMeta(
                    generated_at=now,
                    context=context,
                    sources=[source],
                    warnings=[warning] if warning is not None else [],
                ),
                data=_unavailable_capacity(AdminSourceState.UNAVAILABLE, reason),
            )

        fresh, _ = self._fresh(snapshot.observed_at, now)
        if not fresh:
            reason = "capacity observation exceeded the freshness bound"
            source = self._projection_source("capacity", AdminSourceState.STALE, snapshot.observed_at, now, reason)
            warning = self._projection_warning(source)
            return AdminEnvelope(
                meta=AdminMeta(
                    generated_at=now,
                    context=context,
                    sources=[source],
                    warnings=[warning] if warning is not None else [],
                ),
                data=_unavailable_capacity(AdminSourceState.STALE, reason),
            )

        section_states = (
            ("kubernetes_capacity", snapshot.data.node_pools.state, snapshot.data.node_pools.reason),
            ("kueue", snapshot.data.kueue.state, snapshot.data.kueue.reason),
            ("hpa", snapshot.data.autoscaling.hpa.state, snapshot.data.autoscaling.hpa.reason),
            ("keda", snapshot.data.autoscaling.keda.state, snapshot.data.autoscaling.keda.reason),
            ("node_scaler", snapshot.data.node_scaler.state, snapshot.data.node_scaler.reason),
        )
        sources = [
            self._projection_source(source_id, state, snapshot.observed_at, now, reason)
            for source_id, state, reason in section_states
        ]
        warnings = [warning for source in sources if (warning := self._projection_warning(source)) is not None]
        return AdminEnvelope(
            meta=AdminMeta(generated_at=now, context=context, sources=sources, warnings=warnings),
            data=snapshot.data,
        )

    async def observability(
        self,
        context: AdminContext,
        *,
        model_id: str | None,
        operation_id: UUID | None,
    ) -> AdminEnvelope[AdminObservability]:
        if model_id is not None:
            if _MODEL_SELECTOR.fullmatch(model_id) is None:
                raise AdminProblemError(400, "invalid_model_id", "model identifier is invalid")
            try:
                self.registry.get(model_id, require_enabled=False)
            except KeyError:
                raise AdminProblemError(404, "model_not_found", "model was not found") from None
        now = self.clock().astimezone(UTC)
        try:
            snapshot = await asyncio.wait_for(
                self.observability_adapter.snapshot(
                    context=context,
                    model_id=model_id,
                    operation_id=operation_id,
                ),
                timeout=self.adapter_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except (AdminAdapterUnavailableError, OSError, RuntimeError, TimeoutError, ValueError):
            reason = "observability adapter or query is unavailable"
            source = self._projection_source("observability", AdminSourceState.UNAVAILABLE, None, now, reason)
            warning = self._projection_warning(source)
            return AdminEnvelope(
                meta=AdminMeta(
                    generated_at=now,
                    context=context,
                    sources=[source],
                    warnings=[warning] if warning is not None else [],
                ),
                data=_unavailable_observability(reason),
            )

        fresh, _ = self._fresh(snapshot.observed_at, now)
        state = AdminSourceState.AVAILABLE if fresh else AdminSourceState.STALE
        stale_reason: str | None = None if fresh else "observability observation exceeded the freshness bound"
        source = self._projection_source("observability", state, snapshot.observed_at, now, stale_reason)
        warning = self._projection_warning(source)
        return AdminEnvelope(
            meta=AdminMeta(
                generated_at=now,
                context=context,
                sources=[source],
                warnings=[warning] if warning is not None else [],
            ),
            data=snapshot.data if fresh else _unavailable_observability(stale_reason or "observation is stale"),
        )

    @staticmethod
    def encode_cursor(record: AdminOperationRecord) -> str:
        value = {
            "accepted_at": record.accepted_at.astimezone(UTC).isoformat(),
            "id": str(record.id),
            "v": 1,
        }
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def decode_cursor(value: str) -> tuple[datetime, UUID]:
        if not 1 <= len(value) <= 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise AdminProblemError(400, "invalid_cursor", "operation cursor is invalid")
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            decoded = json.loads(raw)
            if not isinstance(decoded, dict) or set(decoded) != {"accepted_at", "id", "v"} or decoded["v"] != 1:
                raise ValueError
            accepted_at = datetime.fromisoformat(decoded["accepted_at"])
            operation_id = UUID(decoded["id"])
            if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
                raise ValueError
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            raise AdminProblemError(400, "invalid_cursor", "operation cursor is invalid") from None
        return accepted_at.astimezone(UTC), operation_id

    @staticmethod
    def _duration(start: datetime | None, end: datetime | None, *, unavailable_reason: str) -> AdminMeasurement:
        if start is None or end is None:
            return _unavailable("seconds", "postgresql", unavailable_reason)
        return _available(max(0.0, (end - start).total_seconds()), "seconds", "postgresql")

    @classmethod
    def _operation_item(cls, record: AdminOperationRecord) -> AdminOperationItem:
        queue_end = record.activation_started_at or record.started_at
        return AdminOperationItem(
            id=record.id,
            tenant_id=record.tenant_id,
            principal_id=record.principal_id,
            api_key_prefix=record.api_key_prefix,
            model_id=record.model_id,
            model_revision=record.model_revision,
            protocol=record.protocol,
            operation=record.operation,
            status=record.status,
            accepted_at=record.accepted_at,
            completed_at=record.completed_at,
            outcome=record.outcome,
            semantic_outcome=record.semantic_outcome,
            http_status=record.http_status,
            error_class=record.error_code,
            attempt=record.attempt,
            max_attempts=record.max_attempts,
            gpu_count=record.gpu_count,
            preemptible=record.preemptible,
            estimated_gpu_seconds=_estimated(record.estimated_gpu_seconds, "gpu-seconds", "postgresql"),
            input_tokens=(
                _available(float(record.input_tokens), "tokens", "postgresql")
                if record.input_tokens is not None
                else _unavailable("tokens", "postgresql", "runtime did not report input token usage")
            ),
            output_tokens=(
                _available(float(record.output_tokens), "tokens", "postgresql")
                if record.output_tokens is not None
                else _unavailable("tokens", "postgresql", "runtime did not report output token usage")
            ),
            timings=AdminOperationTiming(
                queue_seconds=cls._duration(
                    record.accepted_at,
                    queue_end,
                    unavailable_reason="queue completion timestamp is not recorded yet",
                ),
                cold_start_seconds=(
                    _available(record.cold_start_seconds, "seconds", "postgresql")
                    if record.cold_start_seconds is not None
                    else _unavailable("seconds", "postgresql", "cold-start timing is unavailable")
                ),
                inference_seconds=cls._duration(
                    record.started_at,
                    record.completed_at,
                    unavailable_reason="inference interval is incomplete",
                ),
                total_seconds=cls._duration(
                    record.accepted_at,
                    record.completed_at,
                    unavailable_reason="operation has not reached a terminal timestamp",
                ),
                ttft_seconds=_unavailable("seconds", "postgresql", "TTFT is not instrumented"),
            ),
        )

    async def operation_list(
        self,
        context: AdminContext,
        *,
        limit: int,
        cursor: str | None,
        tenant_id: str | None,
        model_id: str | None,
        principal_id: str | None,
        api_key_prefix: str | None,
        status: OperationStatus | None,
        error_code: str | None,
    ) -> AdminEnvelope[AdminOperationList]:
        after_at: datetime | None = None
        after_id: UUID | None = None
        if cursor is not None:
            after_at, after_id = self.decode_cursor(cursor)
            if not context.from_at <= after_at < context.to_at:
                raise AdminProblemError(400, "invalid_cursor", "operation cursor is outside the selected window")
        query = AdminOperationQuery(
            from_at=context.from_at,
            to_at=context.to_at,
            limit=limit + 1,
            after_at=after_at,
            after_id=after_id,
            tenant_id=tenant_id,
            model_id=model_id,
            principal_id=principal_id,
            api_key_prefix=api_key_prefix,
            status=status,
            error_code=error_code,
        )
        try:
            records = await self.store.admin_list_operations(query)
        except (OSError, RuntimeError, ValueError):
            raise AdminProblemError(503, "operations_unavailable", "operation reporting is unavailable") from None
        has_more = len(records) > limit
        page = records[:limit]
        next_cursor = self.encode_cursor(page[-1]) if has_more and page else None
        now = self.clock().astimezone(UTC)
        source = AdminSource(id="postgresql", state=AdminSourceState.AVAILABLE, observed_at=now, age_seconds=0)
        return AdminEnvelope(
            meta=AdminMeta(generated_at=now, context=context, sources=[source]),
            data=AdminOperationList(items=[self._operation_item(row) for row in page], next_cursor=next_cursor),
        )

    async def operation_detail(
        self,
        context: AdminContext,
        operation_id: UUID,
        *,
        tenant_id: str | None = None,
    ) -> AdminEnvelope[AdminOperationDetail]:
        try:
            record = await self.store.admin_get_operation(operation_id, tenant_id=tenant_id)
        except NotFoundError:
            raise AdminProblemError(404, "operation_not_found", "operation was not found") from None
        except (OSError, RuntimeError, ValueError):
            raise AdminProblemError(503, "operations_unavailable", "operation reporting is unavailable") from None
        now = self.clock().astimezone(UTC)
        return AdminEnvelope(
            meta=AdminMeta(
                generated_at=now,
                context=context,
                sources=[
                    AdminSource(
                        id="postgresql",
                        state=AdminSourceState.AVAILABLE,
                        observed_at=now,
                        age_seconds=0,
                    )
                ],
            ),
            data=AdminOperationDetail(operation=self._operation_item(record)),
        )
