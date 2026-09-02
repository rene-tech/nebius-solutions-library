"""Durable T0 admission, deadline-aware retries, and fenced HA workers."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from .activation_contract import ActivationContractError, ScaleContract
from .lifecycle import (
    LifecycleClock,
    LifecycleCorrelation,
    LifecycleEdge,
    LifecyclePhase,
    LifecycleRepository,
    LifecycleSignal,
    LifecycleSource,
    LifecycleSubject,
    MeasurementQuality,
    NullLifecycleRepository,
    WorkloadTelemetryKind,
    api_key_id_hash,
    payload_shape,
    reproducibility_metadata,
    trace_identity,
)
from .models import (
    ActivationIntentStatus,
    AdmissionRequest,
    ClaimedOperation,
    DynamicAdmissionFence,
    OperationStatus,
    OperationView,
    Principal,
    RuntimeIdentity,
)
from .registry import OperationalModel, Registry
from .runtime import ActivationError, PreemptedError, RouteUnavailableError, RuntimeClient, RuntimeOperationError
from .store import ConflictError, StaleLeaseError, Store
from .telemetry import Metrics

LOGGER = logging.getLogger(__name__)


def _publication_surface(*, protocol: str, required_scope: str) -> str:
    """Map an admission transport to the configured publication surface.

    ModelDeployment exposes OpenAI and MCP independently; it has no separate
    native publication bit. Native HTTP routes are the transport for models
    published through the MCP/model-tool surface, such as Cosmos.
    """

    if required_scope == "mcp.invoke" or protocol == "native":
        return "mcp"
    return "openai"


class AdmissionService:
    def __init__(
        self,
        *,
        registry: Registry,
        store: Store,
        runtime: RuntimeClient,
        metrics: Metrics,
        worker_concurrency: int,
        poll_seconds: float,
        lease_seconds: float,
        maintenance_interval_seconds: float,
        shutdown_grace_seconds: float,
        max_sync_waiters: int = 32,
        wait_poll_initial_seconds: float = 0.05,
        wait_poll_max_seconds: float = 0.5,
        route_refresh: Callable[[], Awaitable[bool]] | None = None,
        lifecycle: LifecycleRepository | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.runtime = runtime
        self.metrics = metrics
        self.worker_concurrency = worker_concurrency
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.maintenance_interval_seconds = maintenance_interval_seconds
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self.max_sync_waiters = max_sync_waiters
        self.wait_poll_initial_seconds = wait_poll_initial_seconds
        self.wait_poll_max_seconds = wait_poll_max_seconds
        self.route_refresh = route_refresh
        self.lifecycle = lifecycle or NullLifecycleRepository()
        self._wake = asyncio.Event()
        self._stop_claiming = asyncio.Event()
        self._stop_maintenance = asyncio.Event()
        self._workers: list[asyncio.Task[None]] = []
        self._maintenance_task: asyncio.Task[None] | None = None
        self._inflight: dict[str, ClaimedOperation] = {}
        self._worker_health: dict[str, bool] = {}
        self._fatal_workers: set[str] = set()
        self._maintenance_healthy = False
        self._waiter_lock = asyncio.Lock()
        self._active_waiters = 0
        self._tracer = trace.get_tracer("fs2_serve.admission")

    async def start(self) -> None:
        if self._workers:
            return
        self._stop_claiming.clear()
        self._stop_maintenance.clear()
        instance = socket.gethostname()
        worker_ids = [f"{instance}:{index}" for index in range(self.worker_concurrency)]
        self._worker_health = {worker_id: False for worker_id in worker_ids}
        self._fatal_workers.clear()
        self._maintenance_healthy = False
        self._workers = [
            asyncio.create_task(self._supervise_worker(worker_id), name=f"fs2-worker-{index}")
            for index, worker_id in enumerate(worker_ids)
        ]
        self._maintenance_task = asyncio.create_task(self._supervise_maintenance(), name="fs2-maintenance")

    async def stop(self) -> None:
        """Stop claims, drain in-flight operations, then fenced-requeue leftovers."""

        self._stop_claiming.set()
        self._stop_maintenance.set()
        self._wake.set()
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maintenance_task
            self._maintenance_task = None
        if self._workers:
            _, pending = await asyncio.wait(self._workers, timeout=self.shutdown_grace_seconds)
            for task in pending:
                task.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._worker_health.clear()

    def health(self) -> dict[str, object]:
        """Return payload-free worker/janitor readiness state."""

        task_health = {
            task.get_name(): not task.done() and self._worker_health.get(worker_id, False)
            for task, worker_id in zip(self._workers, self._worker_health, strict=False)
        }
        maintenance_alive = self._maintenance_task is not None and not self._maintenance_task.done()
        workers_healthy = bool(task_health) and all(task_health.values())
        janitor_healthy = maintenance_alive and self._maintenance_healthy
        return {
            "ready": workers_healthy and janitor_healthy and not self._stop_claiming.is_set(),
            "workers_healthy": workers_healthy,
            "janitor_healthy": janitor_healthy,
            "workers": task_health,
            "inflight": len(self._inflight),
            "sync_waiters": self._active_waiters,
            "max_sync_waiters": self.max_sync_waiters,
            "fatal_workers": len(self._fatal_workers),
        }

    def live(self) -> bool:
        """Fail liveness after a worker cannot durably fence/release its claim."""

        return not self._fatal_workers

    async def close(self) -> None:
        await self.stop()
        await self.runtime.close()

    async def admit(
        self,
        principal: Principal,
        admission: AdmissionRequest,
        *,
        required_scope: str = "inference.invoke",
    ) -> OperationView:
        principal.require(required_scope)
        routes_fresh = True
        if self.route_refresh is not None:
            routes_fresh = await self.route_refresh()
        model = self.registry.get(admission.model_id)
        if not routes_fresh and model.dynamic_policy is not None:
            raise RuntimeError("dynamic model route evidence is unavailable")
        self.registry.authorize_principal(
            model,
            principal,
            requested_model_id=admission.model_id,
            surface=_publication_surface(
                protocol=admission.protocol,
                required_scope=required_scope,
            ),
        )
        self.registry.authorize(model, principal.scopes)
        if admission.operation not in model.gateway.policy_operations:
            raise PermissionError("operation is outside model policy")
        if admission.protocol not in model.gateway.protocols:
            raise ValueError("model does not implement requested protocol")
        request_body = admission.request_body
        trace_carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(trace_carrier)
        continued_traceparent = trace_carrier.get("traceparent")
        if trace_identity(continued_traceparent)[0] is None:
            continued_traceparent = admission.traceparent
        if admission.protocol.startswith("openai-"):
            try:
                payload = json.loads(request_body)
                if not isinstance(payload, dict):
                    raise ValueError
                payload["model"] = model.id
                request_body = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError):
                raise ValueError("OpenAI request payload is not canonical JSON") from None
        canonical_admission = admission.model_copy(
            update={
                "model_id": model.id,
                "request_body": request_body,
                "traceparent": continued_traceparent,
            }
        )
        dynamic_policy = model.dynamic_policy
        if dynamic_policy is not None:
            queue_deadline = datetime.now(UTC) + timedelta(seconds=dynamic_policy.max_queue_seconds)
            if canonical_admission.deadline_at is None or canonical_admission.deadline_at > queue_deadline:
                canonical_admission = canonical_admission.model_copy(update={"deadline_at": queue_deadline})
        operation = await self.store.append_operation(
            principal=principal,
            admission=canonical_admission,
            model_revision=model.model_revision,
            reserved_gpu_seconds=model.gpu_seconds_reservation,
            max_attempts=model.max_attempts,
            dispatch_snapshot=self.registry.dispatch_snapshot(model),
            dynamic_fence=(
                None
                if dynamic_policy is None
                else DynamicAdmissionFence(
                    namespace=dynamic_policy.deployment_namespace,
                    name=dynamic_policy.deployment_name,
                    etag=dynamic_policy.etag,
                )
            ),
        )
        trace_id, parent_span_id = trace_identity(canonical_admission.traceparent)
        subject = LifecycleSubject(
            subject_id=operation.id,
            workload_kind=WorkloadTelemetryKind.ONLINE,
            operation_id=operation.id,
            request_id=operation.id,
            workload_id=operation.id,
            tenant_id=operation.tenant_id,
            principal_id=operation.principal_id,
            api_key_id=operation.token_id,
            api_key_fingerprint=api_key_id_hash(operation.token_id),
            model_id=operation.model_id,
            model_revision=operation.model_revision,
            protocol=operation.protocol,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            accepted_at=operation.accepted_at,
            reproducibility=reproducibility_metadata(
                canonical_admission.request_body,
                canonical_admission.request_content_type,
            ),
        )
        await self.lifecycle.register_subject(subject)
        await self.lifecycle.append_signals(
            [
                LifecycleSignal(
                    event_key=f"online:{operation.id}:{phase.value}",
                    subject_id=operation.id,
                    occurred_at=operation.accepted_at,
                    observed_at=operation.accepted_at,
                    source=LifecycleSource.APPLICATION,
                    quality=MeasurementQuality.APPLICATION_OBSERVED,
                    phase=phase,
                    edge=LifecycleEdge.INSTANT,
                    clock=LifecycleClock.LIFECYCLE,
                )
                for phase in (LifecyclePhase.RECEIVE, LifecyclePhase.ENQUEUE)
            ]
        )
        self._wake.set()
        return operation

    async def wait(self, operation_id: UUID, *, tenant_id: str, seconds: float) -> OperationView:
        """Wait within a bounded per-replica slot using exponentially backed-off reads.

        Admission has already committed before this method runs. When every wait
        slot is occupied, return the durable row immediately so the caller gets
        a recoverable 202 instead of an untracked overload error.
        """

        operation = await self.store.get_operation(operation_id, tenant_id=tenant_id)
        if operation.status.terminal or seconds <= 0:
            return operation

        acquired = False
        async with self._waiter_lock:
            if self._active_waiters >= self.max_sync_waiters:
                self.metrics.sync_wait_saturated.inc()
            else:
                self._active_waiters += 1
                self.metrics.sync_waiters.set(self._active_waiters)
                acquired = True

        if not acquired:
            return operation

        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + seconds
            delay = self.wait_poll_initial_seconds
            while not operation.status.terminal:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(delay, remaining))
                operation = await self.store.get_operation(operation_id, tenant_id=tenant_id)
                delay = min(self.wait_poll_max_seconds, delay * 2)
            return operation
        finally:
            async with self._waiter_lock:
                self._active_waiters -= 1
                self.metrics.sync_waiters.set(self._active_waiters)

    @staticmethod
    async def _backoff(stop: asyncio.Event, failures: int, base_seconds: float) -> None:
        delay = min(5.0, max(0.05, base_seconds) * (2 ** min(failures - 1, 6)))
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def _release_with_retries(self, claimed: ClaimedOperation) -> bool:
        """Retry one fenced release without allowing this worker to claim again."""

        for failure in range(1, 7):
            try:
                await self.store.release_operation(
                    claimed.id,
                    worker_id=claimed.worker_id,
                    fencing_token=claimed.fencing_token,
                )
                return True
            except (StaleLeaseError, ConflictError):
                return True
            except Exception:  # pragma: no cover - transport availability boundary
                LOGGER.error(
                    "fenced operation release unavailable",
                    extra={"operation_id": str(claimed.id), "worker_id": claimed.worker_id, "attempt": failure},
                )
                if failure < 6:
                    await asyncio.sleep(min(2.0, 0.05 * (2 ** (failure - 1))))
        return False

    async def _release_claim(self, claimed: ClaimedOperation) -> bool:
        """Fenced release after runtime and heartbeat children have stopped."""

        release = asyncio.create_task(self._release_with_retries(claimed), name=f"fs2-release-{claimed.id}")
        try:
            return await asyncio.shield(release)
        except asyncio.CancelledError:
            # Rolling shutdown cannot orphan the already-started fenced release.
            try:
                result = await asyncio.shield(release)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive release-task boundary
                self._fatal_workers.add(claimed.worker_id)
                LOGGER.error(
                    "fenced operation release task failed",
                    extra={"operation_id": str(claimed.id), "worker_id": claimed.worker_id},
                )
            else:
                if not result:
                    self._fatal_workers.add(claimed.worker_id)
            raise
        except Exception:  # pragma: no cover - defensive release-task boundary
            # _release_with_retries contains normal store transport failures.
            # If the release task itself still escapes, retain the claim and
            # force liveness failure rather than leaving a dead-but-live Pod.
            self._fatal_workers.add(claimed.worker_id)
            LOGGER.error(
                "fenced operation release task failed",
                extra={"operation_id": str(claimed.id), "worker_id": claimed.worker_id},
            )
            return False

    async def _run_claim(self, worker_id: str, claimed: ClaimedOperation) -> None:
        self._inflight[worker_id] = claimed
        clear_claim = False
        try:
            carrier = {"traceparent": claimed.traceparent} if claimed.traceparent is not None else {}
            parent_context = TraceContextTextMapPropagator().extract(carrier=carrier)
            with self._tracer.start_as_current_span(
                "fs2.operation",
                context=parent_context,
                kind=SpanKind.CONSUMER,
            ) as span:
                span.set_attribute("fs2.operation.id", str(claimed.id))
                span.set_attribute("fs2.request.id", str(claimed.id))
                span.set_attribute("fs2.workload.id", str(claimed.id))
                span.set_attribute("fs2.tenant.id", claimed.tenant_id)
                span.set_attribute("fs2.principal.id", claimed.principal_id)
                span.set_attribute("fs2.api_key.id_hash", api_key_id_hash(claimed.token_id))
                span.set_attribute("fs2.model.id", claimed.model_id)
                span.set_attribute("fs2.model.revision", claimed.model_revision)
                span.set_attribute("fs2.attempt.number", claimed.attempt)
                await self._record_claim(claimed)
                await self._execute(claimed)
            clear_claim = True
        except StaleLeaseError:
            clear_claim = True
            return
        except asyncio.CancelledError:
            # _execute has cancelled and awaited runtime plus heartbeat before
            # control reaches this fenced release.
            released = await self._release_claim(claimed)
            clear_claim = released
            if not released:
                self._fatal_workers.add(worker_id)
            raise
        except Exception:  # pragma: no cover - worker-continuity boundary
            # Never render an arbitrary exception: SDK errors can embed payloads.
            LOGGER.error(
                "worker execution failed; operation will be fenced and retried",
                extra={"operation_id": str(claimed.id), "model_id": claimed.model_id},
            )
            clear_claim = await self._release_claim(claimed)
            if not clear_claim:
                self._fatal_workers.add(worker_id)
        finally:
            # A shutting-down claim is not cleared until child cancellation and
            # the fenced release above have both completed.
            if clear_claim:
                self._inflight.pop(worker_id, None)

    async def _supervise_worker(self, worker_id: str) -> None:
        failures = 0
        try:
            while not self._stop_claiming.is_set():
                try:
                    claimed = await self.store.claim_operation(worker_id, lease_seconds=self.lease_seconds)
                    self._worker_health[worker_id] = True
                    failures = 0
                    if claimed is None:
                        self._wake.clear()
                        try:
                            await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
                        except TimeoutError:
                            pass
                        continue
                    await self._run_claim(worker_id, claimed)
                    if worker_id in self._inflight:
                        # Exhausted or unexpectedly failed release retains the
                        # only authoritative claim. Never claim more work from
                        # this replica; fail liveness so Kubernetes restarts it.
                        self._fatal_workers.add(worker_id)
                        self._worker_health[worker_id] = False
                        LOGGER.error("worker retained an unreleased fenced claim", extra={"worker_id": worker_id})
                        return
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - store availability boundary
                    failures += 1
                    self._worker_health[worker_id] = False
                    LOGGER.error("worker claim loop unavailable", extra={"worker_id": worker_id})
                    if worker_id in self._inflight:
                        # A failed fenced release must never be hidden by a new claim.
                        self._fatal_workers.add(worker_id)
                        return
                    await self._backoff(self._stop_claiming, failures, self.poll_seconds)
        finally:
            self._worker_health[worker_id] = False

    async def _supervise_maintenance(self) -> None:
        failures = 0
        while not self._stop_maintenance.is_set():
            try:
                await self.store.expire_deadline_operations()
                await self.store.reap_stale_operations()
                self.metrics.set_terminal_accounting(await self.store.terminal_accounting())
                failures = 0
                self._maintenance_healthy = True
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - store availability boundary
                failures += 1
                self._maintenance_healthy = False
                LOGGER.error("maintenance loop unavailable")
                await self._backoff(self._stop_maintenance, failures, self.maintenance_interval_seconds)
                continue
            try:
                await asyncio.wait_for(self._stop_maintenance.wait(), timeout=self.maintenance_interval_seconds)
            except TimeoutError:
                pass
        self._maintenance_healthy = False

    @staticmethod
    def _remaining(operation: ClaimedOperation) -> float | None:
        if operation.deadline_at is None:
            return None
        return (operation.deadline_at - datetime.now(UTC)).total_seconds()

    async def _heartbeat(self, operation: ClaimedOperation) -> None:
        interval = max(0.1, self.lease_seconds / 3)
        while True:
            remaining = self._remaining(operation)
            if remaining is not None and remaining <= 0:
                raise StaleLeaseError("operation deadline elapsed")
            await asyncio.sleep(min(interval, remaining) if remaining is not None else interval)
            await self.store.heartbeat(
                operation.id,
                worker_id=operation.worker_id,
                fencing_token=operation.fencing_token,
                lease_seconds=self.lease_seconds,
            )

    async def _execute(self, claimed: ClaimedOperation) -> None:
        work = asyncio.create_task(self._execute_claim(claimed), name=f"fs2-operation-{claimed.id}")
        heartbeat = asyncio.create_task(self._heartbeat(claimed), name=f"fs2-heartbeat-{claimed.id}")
        try:
            done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                heartbeat_error = heartbeat.exception()
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work
                if heartbeat_error is None:
                    raise StaleLeaseError("operation heartbeat stopped before work completed")
                raise heartbeat_error
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await work
        finally:
            for task in (work, heartbeat):
                if not task.done():
                    task.cancel()
            for task in (work, heartbeat):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    @staticmethod
    def _retry_at(model: OperationalModel, claimed: ClaimedOperation) -> datetime:
        base = model.retry_base_seconds * (2 ** max(0, claimed.attempt - 1))
        bounded = min(base, 60.0)
        digest = hashlib.sha256(f"{claimed.id}:{claimed.attempt}".encode()).digest()
        jitter = 0.75 + (int.from_bytes(digest[:2], "big") / 65535) * 0.5
        return datetime.now(UTC) + timedelta(seconds=bounded * jitter)

    async def _retry(self, model: OperationalModel, claimed: ClaimedOperation, exc: RuntimeOperationError) -> bool:
        if claimed.attempt >= claimed.max_attempts:
            return False
        available_at = self._retry_at(model, claimed)
        if claimed.deadline_at is not None and available_at >= claimed.deadline_at:
            return False
        await self.store.retry_operation(
            claimed.id,
            worker_id=claimed.worker_id,
            fencing_token=claimed.fencing_token,
            available_at=available_at,
            error_code=exc.code,
            error_detail=exc.code,
        )
        await self._try_lifecycle(
            claimed.id,
            self.lifecycle.append_signals(
                [
                    LifecycleSignal(
                        event_key=f"online:{claimed.id}:attempt:{claimed.attempt}:retry",
                        subject_id=claimed.id,
                        occurred_at=datetime.now(UTC),
                        observed_at=datetime.now(UTC),
                        source=LifecycleSource.APPLICATION,
                        quality=MeasurementQuality.APPLICATION_OBSERVED,
                        phase=LifecyclePhase.RETRY,
                        edge=LifecycleEdge.INSTANT,
                        clock=LifecycleClock.LIFECYCLE,
                        attempt=claimed.attempt,
                        detail={"reason_code": exc.code},
                    )
                ]
            ),
        )
        self._wake.set()
        return True

    async def _terminal_failure(
        self,
        claimed: ClaimedOperation,
        exc: RuntimeOperationError,
        *,
        status: OperationStatus = OperationStatus.FAILED,
    ) -> OperationView:
        return await self.store.complete_operation(
            claimed.id,
            status=status,
            outcome="preempted" if status == OperationStatus.PREEMPTED else "failed",
            semantic_outcome="not_evaluated",
            http_status=exc.status_code,
            response_body=None,
            response_content_type=None,
            error_code=exc.code,
            error_detail=exc.code,
            runtime=RuntimeIdentity(),
            worker_id=claimed.worker_id,
            fencing_token=claimed.fencing_token,
        )

    async def _execute_claim(self, claimed: ClaimedOperation) -> None:
        model: OperationalModel | None = None
        result_body: bytes | None = None
        result_content_type: str | None = None
        try:
            model = await self._current_model(claimed)
            if model.binding.backend_class == "local-kubernetes" and model.binding.activation.enabled:
                await self._await_activation(model, claimed)
                model = await self._current_model(claimed)
            await self.runtime.activate(model, claimed)
            await self.store.mark_ready(claimed.id, worker_id=claimed.worker_id, fencing_token=claimed.fencing_token)
            await self.store.mark_running(
                claimed.id,
                RuntimeIdentity(),
                worker_id=claimed.worker_id,
                fencing_token=claimed.fencing_token,
            )
            request_body = await self.store.read_request_payload(
                claimed.id, worker_id=claimed.worker_id, fencing_token=claimed.fencing_token
            )
            try:
                model = await self._current_model(claimed)
                invocation_started = datetime.now(UTC)
                with self._tracer.start_as_current_span("fs2.runtime.invoke", kind=SpanKind.CLIENT) as span:
                    span.set_attribute("fs2.operation.id", str(claimed.id))
                    span.set_attribute("fs2.request.id", str(claimed.id))
                    span.set_attribute("fs2.workload.id", str(claimed.id))
                    span.set_attribute("fs2.tenant.id", claimed.tenant_id)
                    span.set_attribute("fs2.principal.id", claimed.principal_id)
                    span.set_attribute("fs2.api_key.id_hash", api_key_id_hash(claimed.token_id))
                    span.set_attribute("fs2.model.id", claimed.model_id)
                    span.set_attribute("fs2.model.revision", claimed.model_revision)
                    span.set_attribute("fs2.attempt.number", claimed.attempt)
                    span.set_attribute("fs2.protocol", claimed.protocol)
                    result = await self.runtime.invoke(model, claimed, request_body)
                    span.set_attribute("fs2.runtime.http_status", result.status_code)
                    span.set_attribute("fs2.runtime.semantic_outcome", result.semantic_outcome)
                invocation_finished = datetime.now(UTC)
                result_body = result.body
                result_content_type = result.content_type
                await self._record_runtime_observation(
                    claimed,
                    result.runtime,
                    started_at=invocation_started,
                    completed_at=invocation_finished,
                )
            finally:
                del request_body
            if result.status_code >= 500:
                failure = RuntimeOperationError("upstream returned a retryable status")
                if await self._retry(model, claimed, failure):
                    return
            success = 200 <= result.status_code < 300 and result.semantic_outcome == "protocol_valid"
            final = await self.store.complete_operation(
                claimed.id,
                status=OperationStatus.SUCCEEDED if success else OperationStatus.FAILED,
                outcome="succeeded" if success else "upstream_failed",
                semantic_outcome=result.semantic_outcome,
                http_status=result.status_code,
                response_body=result.body if success else None,
                response_content_type=result.content_type if success else None,
                error_code=None if success else result.failure_code or "upstream_failure",
                error_detail=None,
                runtime=result.runtime,
                worker_id=claimed.worker_id,
                fencing_token=claimed.fencing_token,
                usage=result.usage,
            )
        except PreemptedError as exc:
            if model is not None and await self._retry(model, claimed, exc):
                return
            final = await self._terminal_failure(claimed, exc, status=OperationStatus.PREEMPTED)
        except RuntimeOperationError as exc:
            if model is not None and await self._retry(model, claimed, exc):
                return
            final = await self._terminal_failure(claimed, exc)
        except (OSError, TimeoutError):
            failure = RuntimeOperationError("runtime unavailable")
            if model is not None and await self._retry(model, claimed, failure):
                return
            final = await self._terminal_failure(claimed, failure)
        except StaleLeaseError:
            return
        await self._try_lifecycle(
            claimed.id,
            self.lifecycle.reconcile(
                claimed.id,
                terminal=True,
                outcome=final.outcome,
                output_shape=payload_shape(result_body, result_content_type),
            ),
        )
        self.metrics.observe_worker_latency(final)

    async def _record_claim(self, claimed: ClaimedOperation) -> None:
        occurred_at = claimed.activation_started_at or datetime.now(UTC)
        await self._try_lifecycle(
            claimed.id,
            self.lifecycle.append_signals(
                [
                    LifecycleSignal(
                        event_key=f"online:{claimed.id}:attempt:{claimed.attempt}:admit",
                        subject_id=claimed.id,
                        occurred_at=occurred_at,
                        observed_at=occurred_at,
                        source=LifecycleSource.APPLICATION,
                        quality=MeasurementQuality.APPLICATION_OBSERVED,
                        phase=LifecyclePhase.ADMIT,
                        edge=LifecycleEdge.INSTANT,
                        clock=LifecycleClock.LIFECYCLE,
                        attempt=claimed.attempt,
                    )
                ]
            ),
        )

    async def _record_runtime_observation(
        self,
        claimed: ClaimedOperation,
        runtime: RuntimeIdentity,
        *,
        started_at: datetime,
        completed_at: datetime,
    ) -> None:
        correlations: list[LifecycleCorrelation] = []
        signals: list[LifecycleSignal] = []
        if runtime.pod_uid is not None or runtime.node_uid is not None or runtime.gpu_uuids:
            gpu_values: list[tuple[str | None, int | None]] = (
                [(gpu_uuid, rank) for rank, gpu_uuid in enumerate(runtime.gpu_uuids)]
                if runtime.gpu_uuids
                else [(None, None)]
            )
            for gpu_uuid, rank in gpu_values:
                suffix = f":gpu:{gpu_uuid}:{rank}" if gpu_uuid is not None else ":pod"
                correlations.append(
                    LifecycleCorrelation(
                        correlation_key=f"online:{claimed.id}:attempt:{claimed.attempt}:runtime{suffix}",
                        subject_id=claimed.id,
                        observed_at=completed_at,
                        source=LifecycleSource.APPLICATION,
                        attempt=claimed.attempt,
                        pod_uid=runtime.pod_uid,
                        node_uid=runtime.node_uid,
                        gpu_uuid=gpu_uuid,
                        gpu_rank=rank,
                    )
                )
        # Active-compute facts require at least a trusted Pod identity. They do
        # not invent scheduler/device allocation clocks from request latency.
        if runtime.pod_uid is not None:
            coordinates: list[tuple[str | None, int | None]] = (
                [(gpu_uuid, rank) for rank, gpu_uuid in enumerate(runtime.gpu_uuids)]
                if runtime.gpu_uuids
                else [(None, None)]
            )
            for gpu_uuid, rank in coordinates:
                interval_key = f"online:{claimed.id}:attempt:{claimed.attempt}:active:{gpu_uuid or 'pod'}:{rank or 0}"
                for edge, occurred_at in (
                    (LifecycleEdge.START, started_at),
                    (LifecycleEdge.END, completed_at),
                ):
                    signals.append(
                        LifecycleSignal(
                            event_key=f"{interval_key}:{edge.value}",
                            subject_id=claimed.id,
                            occurred_at=occurred_at,
                            observed_at=completed_at,
                            source=LifecycleSource.APPLICATION,
                            source_resolution_seconds=0.001,
                            quality=MeasurementQuality.APPLICATION_OBSERVED,
                            phase=LifecyclePhase.ACTIVE_COMPUTE,
                            edge=edge,
                            clock=LifecycleClock.PHASE,
                            interval_key=interval_key,
                            attempt=claimed.attempt,
                            gpu_count=1 if gpu_uuid is not None else 0,
                            pod_uid=runtime.pod_uid,
                            node_uid=runtime.node_uid,
                            gpu_uuid=gpu_uuid,
                            gpu_rank=rank,
                        )
                    )
        if correlations:
            await self._try_lifecycle(claimed.id, self.lifecycle.append_correlations(correlations))
        if signals:
            await self._try_lifecycle(claimed.id, self.lifecycle.append_signals(signals))

    @staticmethod
    async def _try_lifecycle(operation_id: UUID, operation: Awaitable[Any]) -> Any:
        try:
            return await operation
        except Exception:  # pragma: no cover - telemetry must not replay model work
            # Exception strings from a downstream client are not safe log
            # attributes; emit only the opaque operation identity.
            LOGGER.error(
                "lifecycle telemetry write failed after durable admission",
                extra={"operation_id": str(operation_id)},
            )
            return None

    async def _await_activation(self, model: OperationalModel, claimed: ClaimedOperation) -> None:
        """Durably hand local scale-up to the sole Kubernetes mutation owner."""

        intent = await self.store.ensure_activation_intent(
            claimed,
            binding_digest=model.binding.binding_digest,
            worker_id=claimed.worker_id,
            fencing_token=claimed.fencing_token,
        )
        delay = self.wait_poll_initial_seconds
        while intent.status not in {
            ActivationIntentStatus.READY,
            ActivationIntentStatus.FAILED,
            ActivationIntentStatus.EXPIRED,
        }:
            if claimed.deadline_at is not None and claimed.deadline_at <= datetime.now(UTC):
                raise ActivationError("activation intent deadline elapsed")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, self.wait_poll_max_seconds)
            intent = await self.store.get_activation_intent(claimed.id)
        if intent.status is ActivationIntentStatus.READY:
            # Deterministic in-memory worker tests can auto-complete activation
            # without a controller. Every composed gateway/controller runtime
            # supplies ``route_refresh`` and must prove the exact typed target.
            if self.route_refresh is None:
                return
            if self.route_refresh is not None and not await self.route_refresh():
                raise ActivationError("activation route evidence expired before dispatch")
            try:
                current = self.registry.get(model.id)
                contract = ScaleContract.from_model(current)
            except (ActivationContractError, KeyError, RuntimeError):
                raise ActivationError("activation route evidence is unavailable before dispatch") from None
            target = intent.target
            if target is None or not target.active or intent.scale_contract_digest != contract.digest:
                raise ActivationError("activation readiness is not covered by current signed evidence")
            try:
                contract.validate_durable_target(target)
            except ActivationContractError:
                raise ActivationError("activation readiness is not covered by current signed evidence") from None
            return
        raise ActivationError("activation controller did not establish readiness")

    async def _current_model(self, claimed: ClaimedOperation) -> OperationalModel:
        """Reopen route trust immediately before each outbound dispatch."""

        routes_fresh = self.route_refresh is None or await self.route_refresh()
        try:
            model = self.registry.get_revision(
                claimed.model_id,
                claimed.model_revision,
                allow_dynamic=routes_fresh,
            )
        except (KeyError, RuntimeError):
            if not routes_fresh or claimed.dispatch_snapshot is None:
                raise RouteUnavailableError("canonical route evidence is unavailable") from None
            try:
                model = self.registry.restore_dispatch_snapshot(
                    claimed.dispatch_snapshot,
                    model_id=claimed.model_id,
                    revision=claimed.model_revision,
                )
            except (KeyError, RuntimeError, ValueError):
                raise RouteUnavailableError("canonical route evidence is unavailable") from None
        return model
