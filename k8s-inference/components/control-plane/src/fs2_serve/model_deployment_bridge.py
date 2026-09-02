"""Continuously project durable model intent, Kubernetes status, and routes.

The bridge is deliberately small and idempotent. PostgreSQL remains the
desired-state/history authority, the ModelDeployment controller remains the
workload writer, and Registry remains the only public route pointer. Every
refresh reconciles those boundaries in that order and expires its route
snapshot if Kubernetes observations stop arriving.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid5

from .model_deployment import spec_digest
from .model_deployment_admin import StoreModelDeploymentRepository
from .model_deployment_mutation import DesiredWriteError, ModelDeploymentDesiredWriter
from .model_deployment_publication import project_dynamic_publications
from .model_deployment_records import (
    ModelDeploymentObservedStatus,
    ModelDeploymentRevision,
    ModelDeploymentStatusAvailability,
    ModelDeploymentStatusObservation,
    ModelDeploymentStatusView,
)
from .registry import Registry
from .store import ConflictError

LOGGER = logging.getLogger(__name__)
OBSERVATION_NAMESPACE = UUID("4f90028d-d5a6-4cae-a94c-8a095fe3c819")
_CAMEL_WORD_BOUNDARY = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_INITIALISM_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")


class KubernetesModelDeploymentSource(Protocol):
    async def list_models(self) -> list[dict[str, Any]]: ...


def _snake_case(value: str) -> str:
    words = _CAMEL_WORD_BOUNDARY.sub(r"\1_\2", value)
    return _CAMEL_INITIALISM_BOUNDARY.sub(r"\1_\2", words).lower()


def _normalize_keys(value: object) -> object:
    if isinstance(value, Mapping):
        return {_snake_case(str(key)): _normalize_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_keys(item) for item in value]
    return value


def _metadata(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    value = raw.get("metadata")
    return value if isinstance(value, Mapping) else {}


def _status_observation(
    raw: Mapping[str, Any],
    revision: ModelDeploymentRevision,
) -> ModelDeploymentStatusObservation | None:
    metadata = _metadata(raw)
    uid = metadata.get("uid")
    resource_version = metadata.get("resourceVersion")
    annotations = metadata.get("annotations")
    annotations = annotations if isinstance(annotations, Mapping) else {}
    raw_status = raw.get("status")
    if not isinstance(uid, str) or not isinstance(resource_version, str) or not isinstance(raw_status, Mapping):
        return None
    if annotations.get("inference.fs2.nebius.ai/desired-revision") != str(revision.revision):
        return None
    if annotations.get("inference.fs2.nebius.ai/spec-digest") != revision.etag:
        return None
    try:
        status = ModelDeploymentObservedStatus.model_validate(_normalize_keys(raw_status))
    except ValueError:
        return None
    if status.spec_digest != revision.etag:
        return None
    observation_id = uuid5(
        OBSERVATION_NAMESPACE,
        f"{revision.namespace}/{revision.name}:{uid}:{resource_version}:{revision.revision}:{revision.etag}",
    )
    return ModelDeploymentStatusObservation(
        observation_id=observation_id,
        source_uid=uid,
        source_resource_version=resource_version,
        namespace=revision.namespace,
        name=revision.name,
        tenant_id=revision.tenant_id,
        revision=revision.revision,
        status=status,
        observed_at=status.last_reconcile_time,
    )


class ModelDeploymentRuntimeBridge:
    """Bounded eventual reconciler shared safely by multiple API replicas."""

    def __init__(
        self,
        *,
        repository: StoreModelDeploymentRepository,
        writer: ModelDeploymentDesiredWriter,
        source: KubernetesModelDeploymentSource,
        registry: Registry,
        interval_seconds: float = 5,
        route_ttl_seconds: float = 30,
        namespace: str = "fs2-models",
        close_source: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not 1 <= interval_seconds <= 300:
            raise ValueError("model bridge interval is outside the closed bound")
        if not max(interval_seconds * 2, 5) <= route_ttl_seconds <= 900:
            raise ValueError("model bridge route TTL must cover at least two refresh intervals")
        self.repository = repository
        self.writer = writer
        self.source = source
        self.registry = registry
        self.interval_seconds = interval_seconds
        self.route_ttl_seconds = route_ttl_seconds
        self.namespace = namespace
        self.close_source = close_source
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_attempt_monotonic = 0.0
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._route_inventory_fresh = False

    async def _current_revisions(self) -> list[ModelDeploymentRevision]:
        values: list[ModelDeploymentRevision] = []
        after: str | None = None
        while True:
            page = await self.repository.list_current(
                namespace=self.namespace,
                tenant_id=None,
                after_name=after,
                limit=200,
            )
            values.extend(page)
            if len(page) < 200:
                return values
            after = page[-1].name
            if len(values) >= 1000:
                raise ValueError("model bridge desired-state inventory exceeds the configured bound")

    @staticmethod
    def _indexed(raw_models: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in raw_models:
            metadata = _metadata(raw)
            namespace = metadata.get("namespace")
            name = metadata.get("name")
            if not isinstance(namespace, str) or not isinstance(name, str):
                continue
            key = (namespace, name)
            if key in result:
                raise ValueError("Kubernetes ModelDeployment inventory contains a duplicate identity")
            result[key] = raw
        return result

    @staticmethod
    def _desired_matches(raw: Mapping[str, Any] | None, revision: ModelDeploymentRevision) -> bool:
        if raw is None:
            return False
        metadata = _metadata(raw)
        annotations = metadata.get("annotations")
        annotations = annotations if isinstance(annotations, Mapping) else {}
        raw_spec = raw.get("spec")
        try:
            actual_digest = spec_digest(revision.spec.__class__.model_validate(raw_spec))
        except ValueError:
            return False
        return (
            actual_digest == revision.etag
            and annotations.get("inference.fs2.nebius.ai/desired-revision") == str(revision.revision)
            and annotations.get("inference.fs2.nebius.ai/spec-digest") == revision.etag
        )

    async def _refresh_locked(self) -> bool:
        now = datetime.now(UTC)
        revisions = await self._current_revisions()
        try:
            raw_models = await self.source.list_models()
        except DesiredWriteError:
            snapshot = project_dynamic_publications(revisions, {})
            self.registry.set_dynamic_publications(
                snapshot,
                valid_until=now + timedelta(seconds=self.route_ttl_seconds),
            )
            self._last_error = "kubernetes-list-unavailable"
            self._route_inventory_fresh = False
            return False

        indexed = self._indexed(raw_models)
        inventory_fresh = True
        wrote = False
        projection_errors = 0
        for revision in revisions:
            key = (revision.namespace, revision.name)
            if self._desired_matches(indexed.get(key), revision):
                continue
            try:
                await self.writer.apply(revision)
                wrote = True
            except DesiredWriteError:
                projection_errors += 1
        if wrote:
            try:
                indexed = self._indexed(await self.source.list_models())
            except DesiredWriteError:
                indexed = {}
                projection_errors += 1
                inventory_fresh = False

        statuses: dict[tuple[str, str], ModelDeploymentStatusView] = {}
        for revision in revisions:
            key = (revision.namespace, revision.name)
            # Status is evidence only for the exact desired projection.  A CR
            # can retain an apparently ready status while its spec has drifted
            # or an older API replica is still repairing it; publishing that
            # endpoint would bypass the durable desired revision.
            if not self._desired_matches(indexed.get(key), revision):
                continue
            observation = _status_observation(indexed.get(key, {}), revision)
            if observation is None:
                continue
            status_age = now - observation.observed_at
            if status_age > timedelta(seconds=self.route_ttl_seconds) or status_age < -timedelta(
                seconds=self.interval_seconds
            ):
                # A successful Kubernetes list does not refresh an old (or
                # implausibly future) controller observation.  Publishing it
                # would keep a stale Ready endpoint alive indefinitely.
                continue
            try:
                persisted = await self.repository.append_status(observation)
            except ConflictError:
                # A stable Kubernetes resourceVersion produces the same
                # deterministic observation ID on every bridge replica.  The
                # append-only store correctly rejects that duplicate; reload
                # the authoritative row so publication remains stable instead
                # of disappearing on the next poll.
                existing = await self.repository.status(
                    namespace=revision.namespace,
                    name=revision.name,
                    tenant_id=revision.tenant_id,
                )
                if existing is None:
                    continue
                persisted = existing
            if persisted.revision != revision.revision or persisted.status.spec_digest != revision.etag:
                continue
            statuses[key] = ModelDeploymentStatusView(
                namespace=revision.namespace,
                name=revision.name,
                revision=revision.revision,
                etag=revision.etag,
                state=ModelDeploymentStatusAvailability.OBSERVED,
                observation=persisted,
            )

        snapshot = project_dynamic_publications(revisions, statuses)
        valid = self.registry.set_dynamic_publications(
            snapshot,
            valid_until=now + timedelta(seconds=self.route_ttl_seconds),
        )
        if not valid:
            self._last_error = "dynamic-route-projection-invalid"
            self._route_inventory_fresh = False
            return False
        if not inventory_fresh:
            self._last_error = "kubernetes-relist-unavailable"
            self._route_inventory_fresh = False
            return False
        self._last_success_at = now
        self._route_inventory_fresh = True
        if projection_errors:
            self._last_error = "desired-projection-pending"
            return True
        self._last_error = None
        return True

    async def refresh(self, *, force: bool = False) -> bool:
        async with self._lock:
            now_monotonic = time.monotonic()
            if not force and now_monotonic - self._last_attempt_monotonic < min(1.0, self.interval_seconds / 2):
                return self._route_inventory_fresh
            self._last_attempt_monotonic = now_monotonic
            try:
                return await self._refresh_locked()
            except Exception:  # noqa: BLE001 - background reconciliation must survive dependency failures
                self._last_error = "model-bridge-refresh-failed"
                self._route_inventory_fresh = False
                self.registry.invalidate_dynamic_publications()
                LOGGER.exception("dynamic ModelDeployment bridge refresh failed")
                return False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        await self.refresh(force=True)
        self._task = asyncio.create_task(self._run(), name="fs2-model-deployment-runtime-bridge")

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                except TimeoutError:
                    await self.refresh(force=True)
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.close_source is not None:
            await self.close_source()

    def health(self) -> dict[str, object]:
        return {
            "ready": self._last_error is None and self._last_success_at is not None,
            "route_inventory_fresh": self._route_inventory_fresh,
            "publication": self.registry.dynamic_publication_health(),
            "periodic_task_healthy": self._task is not None and not self._task.done(),
            "last_success_at": self._last_success_at.isoformat() if self._last_success_at is not None else None,
            "error": self._last_error,
        }
