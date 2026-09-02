"""Bounded-cardinality metrics and optional OpenTelemetry setup."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, Info, generate_latest

from .models import OperationView, TerminalAccounting
from .registry import OperationalModel


class Metrics:
    _MAX_MODELS = 4096

    def __init__(self, models: Iterable[OperationalModel]) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.request_total = Gauge(
            "fs2_serve_requests_total",
            "Exactly-once durable terminal operations projected from PostgreSQL usage facts",
            ("model", "protocol", "outcome"),
            registry=self.registry,
        )
        self.request_duration_total = Gauge(
            "fs2_serve_terminal_duration_seconds_total",
            "Cumulative T0-to-terminal seconds from durable terminal usage facts",
            ("model", "protocol", "outcome"),
            registry=self.registry,
        )
        self.request_latency = Histogram(
            "fs2_serve_request_duration_seconds",
            "T0 to terminal operation duration",
            ("model", "protocol", "outcome"),
            registry=self.registry,
            buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300, 600, 1800, 3600),
        )
        self.cold_start = Histogram(
            "fs2_serve_cold_start_duration_seconds",
            "Durable T0 to runtime-ready duration",
            ("model", "activation"),
            registry=self.registry,
            buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300, 600, 1800),
        )
        self.gpu_seconds = Gauge(
            "fs2_serve_estimated_gpu_seconds_total",
            "Conservative GPU-seconds estimate charged per claimed attempt; not measured utilization",
            ("model", "gpu_class"),
            registry=self.registry,
        )
        self.queue = Gauge(
            "fs2_serve_operations",
            "Current durable operation rows by bounded state",
            ("model", "state"),
            registry=self.registry,
        )
        self.queue_age = Gauge(
            "fs2_serve_oldest_queued_operation_age_seconds",
            "Age of the oldest durably queued operation",
            ("model",),
            registry=self.registry,
        )
        self.auth_failures = Counter(
            "fs2_serve_authentication_failures_total",
            "Bearer authentication failures by reason class",
            ("reason",),
            registry=self.registry,
        )
        self.sync_waiters = Gauge(
            "fs2_serve_sync_waiters",
            "Synchronous response waiters currently held by this control-plane replica",
            registry=self.registry,
        )
        self.sync_wait_saturated = Counter(
            "fs2_serve_sync_wait_saturated_total",
            "Durably admitted requests returned as operations because synchronous wait slots were full",
            registry=self.registry,
        )
        self.model_info = Info(
            "fs2_serve_model",
            "Bounded canonical model registry metadata, independent of route promotion",
            ("model",),
            registry=self.registry,
        )
        self._models: dict[str, OperationalModel] = {}
        self._accounting_labels: set[tuple[str, str, str]] = set()
        self._queue_models: set[str] = set()
        self.sync_models(models)

    def sync_models(self, models: Iterable[OperationalModel]) -> None:
        """Refresh bounded model metadata after an atomic registry update."""

        values = {model.id: model for model in models}
        if len(values) > self._MAX_MODELS or len(set(self._models) | set(values)) > self._MAX_MODELS:
            raise ValueError("model metric cardinality exceeds the configured bound")
        self._models.update(values)
        self._queue_models.update(values)
        for model in values.values():
            self.model_info.labels(model.id).info(
                {
                    # The canonical catalog can retain blocked records whose
                    # source revision is deliberately not approved yet.  They
                    # are useful inventory, but must not be treated as
                    # routable merely to initialize bounded metadata metrics.
                    "revision": model.gateway.model_revision or "not-pinned",
                    "runtime": model.gateway.runtime_kind,
                    "activation": model.activation_mechanism,
                    "gpu_class": model.gateway.gpu_class,
                }
            )

    def observe_worker_latency(self, operation: OperationView) -> None:
        """Observe non-durable worker latency; never count terminal operations here.

        PostgreSQL ``fs2_usage_facts`` is the only terminal request/accounting
        authority.  Keeping this method explicitly worker-local prevents
        cancellation, revocation, and janitor transitions from being omitted
        by an apparently authoritative completion callback.
        """

        outcome = operation.outcome or str(operation.status)
        labels = (operation.model_id, operation.protocol, outcome)
        if operation.completed_at is not None:
            self.request_latency.labels(*labels).observe(
                max(0, (operation.completed_at - operation.accepted_at).total_seconds())
            )
        model = self._models.get(operation.model_id)
        if model is not None and operation.cold_start_seconds is not None:
            self.cold_start.labels(model.id, model.activation_mechanism).observe(operation.cold_start_seconds)

    def set_terminal_accounting(self, rows: Iterable[TerminalAccounting]) -> None:
        """Set restart-safe cumulative values from the exactly-once ledger."""

        current: set[tuple[str, str, str]] = set()
        gpu_by_model: dict[str, float] = {}
        for row in rows:
            labels = (row.model_id, row.protocol, row.outcome)
            current.add(labels)
            self.request_total.labels(*labels).set(row.operations)
            self.request_duration_total.labels(*labels).set(row.duration_seconds)
            gpu_by_model[row.model_id] = gpu_by_model.get(row.model_id, 0.0) + row.estimated_gpu_seconds
        for labels in self._accounting_labels - current:
            self.request_total.labels(*labels).set(0)
            self.request_duration_total.labels(*labels).set(0)
        self._accounting_labels = current
        for model_id, model in self._models.items():
            self.gpu_seconds.labels(model_id, model.gateway.gpu_class).set(gpu_by_model.get(model_id, 0.0))

    def set_queue(self, counts: dict[tuple[str, str], int]) -> None:
        """Project durable queue demand, including live-added model IDs.

        Dynamic ModelDeployments are admitted after the process-local Metrics
        object is constructed.  KEDA consumes this gauge to activate a cold
        runtime, so limiting the projection to the startup catalog would leave
        every live-added model permanently at zero demand.  Retaining the
        previously observed IDs also lets us explicitly clear their active
        states after the final operation leaves the queue.
        """

        states = ("queued", "activating", "running", "succeeded", "failed", "cancelled", "preempted", "expired")
        observed_models = {model_id for model_id, _state in counts}
        queue_models = self._queue_models | observed_models
        for model in queue_models:
            for state in states:
                self.queue.labels(model, state).set(counts.get((model, state), 0))
        self._queue_models = queue_models

    def set_queue_age(self, ages: dict[str, float]) -> None:
        queue_models = self._queue_models | set(ages)
        if len(queue_models) > self._MAX_MODELS:
            raise ValueError("queue age metric cardinality exceeds the configured bound")
        for model in queue_models:
            self.queue_age.labels(model).set(ages.get(model, 0))
        self._queue_models = queue_models

    def render(self) -> bytes:
        return generate_latest(self.registry)


def configure_tracing(endpoint: str | None) -> Any:
    """Configure one process-wide provider; return a tracer even when export is disabled."""

    provider = TracerProvider(resource=Resource.create({"service.name": "fs2-serve-control-plane"}))
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    return trace.get_tracer("fs2_serve", "0.1.0")
