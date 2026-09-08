"""Observed public transport exchanges, separate from durable logical operations.

No request/response contents, credentials, headers or query strings are retained.
Body sizes are exact ASGI payload bytes (not compressed network-wire sizes). An
unconsumed request or interrupted response has unknown total size, not zero.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

import asyncpg
from pydantic import AwareDatetime, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .models import Principal, StrictModel

LOGGER = logging.getLogger(__name__)
_STATE: ContextVar[dict[str, Any] | None] = ContextVar("fs2_request_telemetry_state", default=None)


class RequestTelemetry(StrictModel):
    request_id: UUID
    started_at: AwareDatetime
    completed_at: AwareDatetime
    endpoint: str
    method: str
    transport: Literal["http", "mcp"]
    http_status: int | None = Field(default=None, ge=100, le=599)
    response_duration_seconds: float = Field(ge=0)
    request_bytes: int | None = Field(default=None, ge=0)
    response_bytes: int | None = Field(default=None, ge=0)
    request_bytes_observed: int = Field(ge=0)
    response_bytes_observed: int = Field(ge=0)
    request_complete: bool
    response_complete: bool
    disconnected: bool
    error_type: str | None = None
    tenant_id: str | None = None
    principal_id: str | None = None
    token_id: UUID | None = None
    model_id: str | None = None
    operation_id: UUID | None = None
    mcp_tool: str | None = None
    mcp_is_error: bool | None = None


class RequestTransportBucket(StrictModel):
    timestamp: AwareDatetime
    request_count: int | None
    status_classes: dict[str, int] | None


class RequestTransportUsage(StrictModel):
    """Only the observed transport window; never an inferred historical total."""

    request_count: int
    completed_response_count: int
    incomplete_response_count: int
    successful_http_count: int
    failed_http_count: int
    mcp_tool_error_count: int
    request_bytes: int | None
    response_bytes: int | None
    request_bytes_known_count: int
    response_bytes_known_count: int
    average_response_duration_seconds: float | None
    maximum_response_duration_seconds: float | None
    first_observed_at: AwareDatetime | None
    last_observed_at: AwareDatetime | None
    status_classes: dict[str, int] = Field(default_factory=dict)
    requests_over_time: list[RequestTransportBucket] = Field(default_factory=list)
    time_bucket_seconds: int = 1


class RequestTelemetryStore(Protocol):
    async def record(self, observation: RequestTelemetry) -> None: ...


class InMemoryRequestTelemetryStore:
    """Local/test evidence only; production uses the PostgreSQL implementation."""

    def __init__(self) -> None:
        self.observations: list[RequestTelemetry] = []

    async def record(self, observation: RequestTelemetry) -> None:
        if all(row.request_id != observation.request_id for row in self.observations):
            self.observations.append(observation)


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _label(value: object, limit: int = 256) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit and value.isprintable() else None


@contextmanager
def request_telemetry_context(request: object) -> Iterator[None]:
    """Bind the SDK's actual HTTP request, including SDK-spawned handler tasks."""

    scope = getattr(request, "scope", None)
    state = scope.setdefault("state", {}) if isinstance(scope, dict) else None
    token = _STATE.set(state) if isinstance(state, dict) else None
    try:
        yield
    finally:
        if token is not None:
            _STATE.reset(token)


def observe_request_metadata(
    *,
    principal: Principal | None = None,
    model_id: str | None = None,
    operation_id: UUID | None = None,
    mcp_tool: str | None = None,
    mcp_is_error: bool | None = None,
) -> None:
    """Called only at existing authentication/admission/result boundaries."""

    state = _STATE.get()
    if state is None:
        return
    if principal is not None:
        state["principal"] = principal
    for key, value in (("model_id", model_id), ("mcp_tool", mcp_tool)):
        if _label(value) is not None:
            state[key] = value
    if operation_id is not None:
        state["operation_id"] = operation_id
    if mcp_is_error is not None:
        state["mcp_is_error"] = mcp_is_error


def observe_mcp_result(result: Any) -> None:
    """Use successful, authorized result identities, never arbitrary input IDs."""

    is_error = getattr(result, "is_error", None)
    if isinstance(result, dict):
        is_error = result.get("isError", result.get("is_error", is_error))
    if isinstance(is_error, bool):
        observe_request_metadata(mcp_is_error=is_error)
    payload = getattr(result, "structured_content", result)
    if not isinstance(payload, dict):
        return
    operation = payload.get("operation", payload)
    if isinstance(operation, dict):
        observe_request_metadata(
            operation_id=_uuid(operation.get("id", operation.get("operation_id"))),
            model_id=_label(operation.get("model_id")),
        )


class RequestTelemetryMiddleware:
    def __init__(self, app: ASGIApp, *, store: RequestTelemetryStore, persist_timeout_seconds: float = 2.0) -> None:
        self.app = app
        self.store = store
        self.persist_timeout_seconds = persist_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path.startswith("/v1/") or path in {"/mcp", "/mcp/"}):
            await self.app(scope, receive, send)
            return
        started_at = datetime.now(UTC)
        started_clock = time.monotonic()
        state = scope.setdefault("state", {})
        token = _STATE.set(state)
        request_bytes = response_bytes = 0
        request_complete = response_complete = disconnected = False
        status: int | None = None
        response_operation: UUID | None = None
        finished_at: datetime | None = None
        finished_clock: float | None = None
        error_type: str | None = None

        async def observed_receive() -> Message:
            nonlocal request_bytes, request_complete, disconnected
            message = await receive()
            if message["type"] == "http.request":
                request_bytes += len(message.get("body", b""))
                request_complete = not message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def observed_send(message: Message) -> None:
            nonlocal status, response_operation, response_bytes, response_complete, finished_at, finished_clock
            # Count only payload actually accepted by the ASGI server's send.
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
                for key, value in message.get("headers", []):
                    if key.lower() == b"x-fs2-operation-id":
                        response_operation = _uuid(value.decode("ascii", errors="ignore"))
            elif message["type"] == "http.response.body":
                response_bytes += len(message.get("body", b""))
                if not message.get("more_body", False):
                    response_complete = True
                    finished_at, finished_clock = datetime.now(UTC), time.monotonic()

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException as error:
            error_type = type(error).__name__
            raise
        finally:
            try:
                principal = state.get("principal")
                if not isinstance(principal, Principal):
                    principal = None
                observation = RequestTelemetry(
                    request_id=uuid4(),
                    started_at=started_at,
                    completed_at=finished_at or datetime.now(UTC),
                    endpoint=path[:1024],
                    method=str(scope.get("method", ""))[:32],
                    transport="mcp" if path in {"/mcp", "/mcp/"} else "http",
                    http_status=status,
                    response_duration_seconds=max(0, (finished_clock or time.monotonic()) - started_clock),
                    request_bytes=request_bytes if request_complete else None,
                    response_bytes=response_bytes if response_complete else None,
                    request_bytes_observed=request_bytes,
                    response_bytes_observed=response_bytes,
                    request_complete=request_complete,
                    response_complete=response_complete,
                    disconnected=disconnected,
                    error_type=error_type,
                    tenant_id=principal.tenant_id if principal else None,
                    principal_id=principal.principal_id if principal else None,
                    token_id=principal.token_id if principal else None,
                    model_id=_label(state.get("model_id")) or _label(scope.get("path_params", {}).get("model_id")),
                    operation_id=_uuid(state.get("operation_id")) or response_operation,
                    mcp_tool=_label(state.get("mcp_tool")),
                    mcp_is_error=state.get("mcp_is_error") if isinstance(state.get("mcp_is_error"), bool) else None,
                )
                try:
                    await asyncio.wait_for(self.store.record(observation), timeout=self.persist_timeout_seconds)
                except Exception as error:
                    # Telemetry failure must not replace a response or expose connection details.
                    LOGGER.warning("request telemetry persistence failed (%s)", type(error).__name__)
            finally:
                _STATE.reset(token)


class PostgresRequestTelemetryStore:
    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self.pool = pool

    async def record(self, observation: RequestTelemetry) -> None:
        values = observation.model_dump()
        columns = tuple(RequestTelemetry.model_fields)
        async with self.pool.acquire() as connection:
            if values["model_id"] is None and values["operation_id"] is not None and values["tenant_id"] is not None:
                values["model_id"] = await connection.fetchval(
                    "SELECT model_id FROM fs2_operations WHERE id=$1 AND tenant_id=$2",
                    values["operation_id"],
                    values["tenant_id"],
                )
            await connection.execute(
                f"INSERT INTO fs2_request_telemetry ({','.join(columns)}) "  # noqa: S608 - fixed model field names
                f"VALUES ({','.join(f'${index}' for index in range(1, len(columns) + 1))}) "
                "ON CONFLICT (request_id) DO NOTHING",
                *(values[column] for column in columns),
            )

    async def for_operation(self, operation_id: UUID, tenant_id: str | None) -> list[RequestTelemetry]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT * FROM fs2_request_telemetry WHERE operation_id=$1 "
                "AND ($2::text IS NULL OR tenant_id=$2) ORDER BY started_at,request_id LIMIT 500",
                operation_id,
                tenant_id,
            )
        return [RequestTelemetry.model_validate(dict(row)) for row in rows]

    async def usage(
        self, model_id: str, from_at: datetime, to_at: datetime, tenant_id: str | None
    ) -> RequestTransportUsage:
        bucket_seconds = max(1, math.ceil((to_at - from_at).total_seconds() / 60))
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """WITH observations AS (
                    SELECT *,CASE WHEN http_status BETWEEN 200 AND 599 THEN (http_status / 100)::text || 'xx'
                        ELSE 'unknown' END AS status_class
                    FROM fs2_request_telemetry WHERE model_id=$1 AND started_at >= $2 AND started_at < $3
                        AND ($4::text IS NULL OR tenant_id=$4)
                ), classes AS (
                    SELECT status_class,count(*) AS count FROM observations GROUP BY status_class
                ), buckets AS (
                    SELECT date_bin($5 * interval '1 second',started_at,$2::timestamptz) AS timestamp,
                        count(*) AS request_count,jsonb_build_object(
                            '2xx',count(*) FILTER(WHERE status_class='2xx'),
                            '3xx',count(*) FILTER(WHERE status_class='3xx'),
                            '4xx',count(*) FILTER(WHERE status_class='4xx'),
                            '5xx',count(*) FILTER(WHERE status_class='5xx'),
                            'unknown',count(*) FILTER(WHERE status_class='unknown')) AS status_classes
                    FROM observations GROUP BY 1
                ), grid AS (
                    SELECT generate_series($2::timestamptz,$3::timestamptz - interval '1 microsecond',
                        $5 * interval '1 second') AS timestamp
                ), timeline AS (
                    SELECT grid.timestamp,buckets.request_count,buckets.status_classes
                    FROM grid LEFT JOIN buckets USING(timestamp)
                ) SELECT count(*) AS request_count,
                    count(*) FILTER (WHERE response_complete) AS completed_response_count,
                    count(*) FILTER (WHERE NOT response_complete) AS incomplete_response_count,
                    count(*) FILTER (WHERE http_status BETWEEN 200 AND 399) AS successful_http_count,
                    count(*) FILTER (WHERE http_status >= 400) AS failed_http_count,
                    count(*) FILTER (WHERE mcp_is_error) AS mcp_tool_error_count,
                    CASE WHEN count(*) = count(request_bytes) THEN sum(request_bytes) END AS request_bytes,
                    CASE WHEN count(*) = count(response_bytes) THEN sum(response_bytes) END AS response_bytes,
                    count(request_bytes) AS request_bytes_known_count,
                    count(response_bytes) AS response_bytes_known_count,
                    avg(response_duration_seconds) FILTER (WHERE response_complete)
                        AS average_response_duration_seconds,
                    max(response_duration_seconds) FILTER (WHERE response_complete)
                        AS maximum_response_duration_seconds,
                    min(started_at) AS first_observed_at,max(started_at) AS last_observed_at,
                    (SELECT coalesce(jsonb_object_agg(status_class,count),'{}') FROM classes) AS status_classes,
                    (SELECT jsonb_agg(to_jsonb(t) ORDER BY timestamp) FROM timeline t) AS requests_over_time
                FROM observations""",
                model_id,
                from_at,
                to_at,
                tenant_id,
                bucket_seconds,
            )
        assert row is not None
        values = dict(row)
        # asyncpg's default JSON codec returns strings; callers with JSON codecs
        # installed already receive decoded objects.
        for key in ("status_classes", "requests_over_time"):
            if isinstance(values[key], str):
                values[key] = json.loads(values[key])
        values["requests_over_time"] = values["requests_over_time"] or []
        values["time_bucket_seconds"] = bucket_seconds
        return RequestTransportUsage.model_validate(values)
