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
SemanticOutcome = Literal["succeeded", "accepted", "failed", "cancelled", "timed_out", "unknown"]
AdmissionStage = Literal["pre_admission", "admitted", "not_applicable", "unknown"]
_SEMANTIC_OUTCOMES = frozenset({"succeeded", "accepted", "failed", "cancelled", "timed_out", "unknown"})
_ADMISSION_STAGES = frozenset({"pre_admission", "admitted", "not_applicable", "unknown"})
_TERMINAL_FAILURES = frozenset({"failed", "cancelled", "expired", "preempted"})
_JSONRPC_ERROR_TYPES = {
    -32700: "jsonrpc_parse_error",
    -32600: "jsonrpc_invalid_request",
    -32601: "jsonrpc_method_not_found",
    -32602: "jsonrpc_invalid_params",
    -32603: "jsonrpc_internal_error",
}
_MAX_SEMANTIC_ENVELOPE_BYTES = 256 * 1024


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
    semantic_outcome: SemanticOutcome | None = None
    jsonrpc_error_code: int | None = Field(default=None, ge=-(2**31), le=2**31 - 1)
    semantic_error_type: str | None = None
    admission_stage: AdmissionStage | None = None


class RequestTransportBucket(StrictModel):
    timestamp: AwareDatetime
    request_count: int | None
    status_classes: dict[str, int] | None
    semantic_outcomes: dict[str, int] | None


class RequestTransportUsage(StrictModel):
    """Only the observed transport window; never an inferred historical total."""

    request_count: int
    completed_response_count: int
    incomplete_response_count: int
    successful_http_count: int
    failed_http_count: int
    mcp_tool_error_count: int
    semantic_success_count: int
    semantic_accepted_count: int
    semantic_failed_count: int
    semantic_cancelled_count: int
    semantic_timed_out_count: int
    semantic_unknown_count: int
    pre_admission_failure_count: int
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


class RequestSemanticMetric(StrictModel):
    model_id: str
    transport: Literal["http", "mcp"]
    mcp_tool: str
    semantic_outcome: SemanticOutcome
    admission_stage: AdmissionStage
    exchanges: int = Field(ge=0)


class InMemoryRequestTelemetryStore:
    """Local/test evidence only; production uses the PostgreSQL implementation."""

    def __init__(self) -> None:
        self.observations: list[RequestTelemetry] = []

    async def record(self, observation: RequestTelemetry) -> None:
        if all(row.request_id != observation.request_id for row in self.observations):
            self.observations.append(observation)

    async def semantic_metric_rows(self) -> list[RequestSemanticMetric]:
        counts: dict[tuple[str, str, str, str, str], int] = {}
        for row in self.observations:
            labels = (
                row.model_id or "unattributed",
                row.transport,
                row.mcp_tool or "none",
                row.semantic_outcome or "unknown",
                row.admission_stage or "unknown",
            )
            counts[labels] = counts.get(labels, 0) + 1
        return [
            RequestSemanticMetric.model_validate(
                {
                    "model_id": model_id,
                    "transport": transport,
                    "mcp_tool": tool,
                    "semantic_outcome": outcome,
                    "admission_stage": stage,
                    "exchanges": count,
                }
            )
            for (model_id, transport, tool, outcome, stage), count in sorted(counts.items())
        ]


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def ensure_request_id(scope: Scope) -> UUID:
    """Allocate one server-owned exchange ID before authentication/dispatch."""
    state = scope.setdefault("state", {})
    identity = _uuid(state.get("fs2_request_id"))
    if identity is None:
        identity = uuid4()
        state["fs2_request_id"] = identity
    return identity


def current_request_id() -> UUID | None:
    """Join synchronous runtime dispatch to the enclosing public exchange."""
    state = _STATE.get()
    return _uuid(state.get("fs2_request_id")) if state is not None else None


def _label(value: object, limit: int = 256) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit and value.isprintable() else None


def _semantic_error_type(value: object) -> str | None:
    """Normalize a safe, bounded diagnostic class; never persist error detail."""

    label = _label(value, 128)
    if label is None:
        return None
    normalized = "".join(character.lower() if character.isalnum() else "_" for character in label).strip("_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized[:96] or None


def _unwrap_result(value: Any) -> Any:
    """Unwrap SDK HandlerResult/RootModel containers without trusting payload fields."""

    current = value
    seen: set[int] = set()
    for _ in range(6):
        if id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, dict):
            return current
        root = getattr(current, "root", None)
        if root is not None and root is not current:
            current = root
            continue
        model_dump = getattr(current, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump(by_alias=True)
            if isinstance(dumped, dict):
                return dumped
        break
    return current


def _operation_payload(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    candidate = payload.get("operation", payload)
    return candidate if isinstance(candidate, dict) else None


def _payload_error(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    candidate = payload.get("error")
    return candidate if isinstance(candidate, dict) else None


def _result_payload(value: Any) -> tuple[dict[str, Any] | None, bool | None]:
    raw = _unwrap_result(value)
    if not isinstance(raw, dict):
        return None, None
    is_error = raw.get("isError", raw.get("is_error"))
    structured = raw.get("structuredContent", raw.get("structured_content"))
    if not isinstance(structured, dict):
        structured = raw
    return structured, is_error if isinstance(is_error, bool) else None


def _semantic_from_payload(payload: dict[str, Any] | None, is_error: bool | None) -> dict[str, Any]:
    error = _payload_error(payload)
    operation = _operation_payload(payload)
    operation_id = _uuid(operation.get("id", operation.get("operation_id"))) if operation else None
    model_id = _label(operation.get("model_id")) if operation else None
    if is_error is True or error is not None:
        return {
            "semantic_outcome": "failed",
            "semantic_error_type": _semantic_error_type(
                error.get("type", error.get("code")) if error is not None else "mcp_tool_error"
            ),
            "admission_stage": "admitted" if operation_id is not None else "pre_admission",
            "operation_id": operation_id,
            "model_id": model_id,
            "mcp_is_error": True,
        }
    status = _label(operation.get("status")) if operation else None
    if status == "succeeded":
        outcome: SemanticOutcome = "succeeded"
    elif status == "expired":
        outcome = "timed_out"
    elif status in _TERMINAL_FAILURES:
        outcome = "cancelled" if status == "cancelled" else "failed"
    elif status in {"queued", "activating", "running"} or operation_id is not None:
        outcome = "accepted"
    else:
        outcome = "succeeded"
    return {
        "semantic_outcome": outcome,
        "semantic_error_type": _semantic_error_type(operation.get("error_class")) if operation else None,
        "admission_stage": "admitted" if operation_id is not None else "not_applicable",
        "operation_id": operation_id,
        "model_id": model_id,
        "mcp_is_error": outcome in {"failed", "cancelled", "timed_out"},
    }


def _json_messages(body: bytes) -> list[dict[str, Any]]:
    if not body or len(body) > _MAX_SEMANTIC_ENVELOPE_BYTES:
        return []
    candidates = [body]
    if body.lstrip().startswith(b"data:") or b"\ndata:" in body:
        candidates = [line[5:].strip() for line in body.splitlines() if line.startswith(b"data:")]
    messages: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate in {b"", b"[DONE]"}:
            continue
        try:
            value = json.loads(candidate)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if isinstance(value, dict):
            messages.append(value)
    return messages


def classify_public_outcome(
    *,
    path: str,
    http_status: int | None,
    response_body: bytes,
    response_complete: bool,
    disconnected: bool,
    process_error_type: str | None,
    state: dict[str, Any],
    operation_id: UUID | None,
) -> dict[str, Any]:
    """Return payload-free semantic fields, preferring trusted handler state."""

    values: dict[str, Any] = {
        "semantic_outcome": state.get("semantic_outcome"),
        "jsonrpc_error_code": state.get("jsonrpc_error_code"),
        "semantic_error_type": state.get("semantic_error_type"),
        "admission_stage": state.get("admission_stage"),
        "mcp_is_error": state.get("mcp_is_error"),
        "operation_id": operation_id,
        "model_id": _label(state.get("model_id")),
    }
    is_mcp = path in {"/mcp", "/mcp/"}
    if is_mcp:
        messages = _json_messages(response_body) if response_complete else []
        protocol_errors = [message["error"] for message in messages if isinstance(message.get("error"), dict)]
        if protocol_errors:
            # A later successful-looking SSE event must never erase a protocol
            # failure from the same exchange. Handler state remains preferred
            # for success detail, but either transport representation of a
            # failure makes the whole customer exchange a failure.
            protocol_error = protocol_errors[-1]
            code = protocol_error.get("code")
            code = code if isinstance(code, int) and not isinstance(code, bool) else None
            data = protocol_error.get("data")
            detail = data if isinstance(data, dict) else {}
            values.update(
                semantic_outcome="failed",
                jsonrpc_error_code=code,
                semantic_error_type=_semantic_error_type(detail.get("type", detail.get("code")))
                or (_JSONRPC_ERROR_TYPES.get(code) if code is not None else None)
                or "jsonrpc_error",
                admission_stage="pre_admission" if operation_id is None else "admitted",
                mcp_is_error=True,
            )
        else:
            trusted_failure = values["semantic_outcome"] in {"failed", "cancelled", "timed_out"}
            for message in messages:
                if "result" not in message:
                    continue
                payload, is_error = _result_payload(message["result"])
                classified = _semantic_from_payload(payload, is_error)
                classified_failure = classified.get("semantic_outcome") in {
                    "failed",
                    "cancelled",
                    "timed_out",
                }
                if not trusted_failure or classified_failure:
                    for key, value in classified.items():
                        if value is not None:
                            values[key] = value
                if classified.get("operation_id") is not None:
                    operation_id = classified["operation_id"]
                    values["operation_id"] = operation_id
                trusted_failure = trusted_failure or classified_failure
        if values["semantic_outcome"] not in _SEMANTIC_OUTCOMES:
            if http_status is not None and http_status >= 400:
                values.update(
                    semantic_outcome="failed",
                    semantic_error_type={
                        401: "authentication_failed",
                        403: "authorization_failed",
                        413: "request_too_large",
                        429: "rate_limit_reached",
                    }.get(http_status, "http_transport_error"),
                    admission_stage="pre_admission" if operation_id is None else "admitted",
                    mcp_is_error=True,
                )
            elif response_complete and http_status is not None and 200 <= http_status < 400:
                if _label(state.get("mcp_tool")) is not None and not messages:
                    values.update(
                        semantic_outcome="failed",
                        semantic_error_type="malformed_mcp_response",
                        admission_stage="pre_admission" if operation_id is None else "admitted",
                        mcp_is_error=True,
                    )
                else:
                    # Notifications and protocol initialization do not admit model work.
                    values.update(semantic_outcome="succeeded", admission_stage="not_applicable", mcp_is_error=False)
            else:
                values.update(semantic_outcome="unknown", admission_stage="unknown")
    else:
        if http_status == 202:
            values.update(semantic_outcome="accepted", admission_stage="admitted" if operation_id else "unknown")
        elif http_status is not None and 200 <= http_status < 400:
            values.update(
                semantic_outcome="succeeded", admission_stage="admitted" if operation_id else "not_applicable"
            )
        elif http_status is not None and http_status >= 400:
            values.update(
                semantic_outcome="failed",
                semantic_error_type="http_request_error",
                admission_stage="admitted" if operation_id else "pre_admission",
            )
        else:
            values.update(semantic_outcome="unknown", admission_stage="unknown")
    # ASGI transports may report http.disconnect after the final response body
    # was accepted by send(). That closes an already completed exchange; it
    # must not erase its JSON-RPC/HTTP outcome. Keep the raw disconnect flag in
    # telemetry/debug records, but classify cancellation only while incomplete.
    if disconnected and not response_complete:
        values.update(
            semantic_outcome="cancelled",
            semantic_error_type="client_disconnected",
            admission_stage="admitted" if operation_id else "pre_admission",
        )
    elif process_error_type:
        timeout = process_error_type in {"TimeoutError", "TimeoutException"}
        values.update(
            semantic_outcome="timed_out" if timeout else "failed",
            semantic_error_type="request_timeout" if timeout else "transport_exception",
            admission_stage="admitted" if operation_id else "pre_admission",
        )
    if values["admission_stage"] not in _ADMISSION_STAGES:
        values["admission_stage"] = "unknown"
    values["semantic_error_type"] = _semantic_error_type(values["semantic_error_type"])
    return values


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
    semantic_outcome: SemanticOutcome | None = None,
    jsonrpc_error_code: int | None = None,
    semantic_error_type: str | None = None,
    admission_stage: AdmissionStage | None = None,
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
    if semantic_outcome in _SEMANTIC_OUTCOMES:
        state["semantic_outcome"] = semantic_outcome
    if isinstance(jsonrpc_error_code, int) and not isinstance(jsonrpc_error_code, bool):
        state["jsonrpc_error_code"] = jsonrpc_error_code
    normalized_error = _semantic_error_type(semantic_error_type)
    if normalized_error is not None:
        state["semantic_error_type"] = normalized_error
    if admission_stage in _ADMISSION_STAGES:
        state["admission_stage"] = admission_stage


def observe_mcp_result(result: Any) -> None:
    """Use successful, authorized result identities, never arbitrary input IDs."""

    payload, is_error = _result_payload(result)
    if isinstance(payload, dict) and isinstance(payload.get("operation"), dict):
        operation = payload["operation"]
        claimed_id = operation.get("id", operation.get("operation_id"))
        if claimed_id is not None and _uuid(claimed_id) is None:
            return
    classified = _semantic_from_payload(payload, is_error)
    observe_request_metadata(**classified)


def observe_mcp_protocol_error(error: Any) -> None:
    """Capture JSON-RPC failures before the SDK serializes them over HTTP 200."""

    code = getattr(error, "code", None)
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    data = getattr(error, "data", None)
    detail = data if isinstance(data, dict) else {}
    operation_id = _uuid(detail.get("operation_id"))
    observe_request_metadata(
        operation_id=operation_id,
        mcp_is_error=True,
        semantic_outcome="failed",
        jsonrpc_error_code=code,
        semantic_error_type=_semantic_error_type(detail.get("type", detail.get("code")))
        or (_JSONRPC_ERROR_TYPES.get(code) if code is not None else None)
        or "jsonrpc_error",
        admission_stage="admitted" if operation_id is not None else "pre_admission",
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
        request_id = ensure_request_id(scope)
        token = _STATE.set(state)
        request_bytes = response_bytes = 0
        request_complete = response_complete = disconnected = False
        status: int | None = None
        response_operation: UUID | None = None
        semantic_response = bytearray()
        semantic_response_overflow = False
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
            nonlocal semantic_response_overflow
            # Count only payload actually accepted by the ASGI server's send.
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
                for key, value in message.get("headers", []):
                    if key.lower() == b"x-fs2-operation-id":
                        response_operation = _uuid(value.decode("ascii", errors="ignore"))
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                response_bytes += len(body)
                if not semantic_response_overflow:
                    remaining = _MAX_SEMANTIC_ENVELOPE_BYTES - len(semantic_response)
                    semantic_response.extend(body[:remaining])
                    semantic_response_overflow = len(body) > remaining
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
                operation_id = _uuid(state.get("operation_id")) or response_operation
                semantic = classify_public_outcome(
                    path=path,
                    http_status=status,
                    response_body=bytes(semantic_response) if not semantic_response_overflow else b"",
                    response_complete=response_complete,
                    disconnected=disconnected,
                    process_error_type=error_type,
                    state=state,
                    operation_id=operation_id,
                )
                observation = RequestTelemetry(
                    request_id=request_id,
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
                    model_id=semantic["model_id"] or _label(scope.get("path_params", {}).get("model_id")),
                    operation_id=semantic["operation_id"],
                    mcp_tool=_label(state.get("mcp_tool")),
                    mcp_is_error=(semantic["mcp_is_error"] if isinstance(semantic.get("mcp_is_error"), bool) else None),
                    semantic_outcome=semantic["semantic_outcome"],
                    jsonrpc_error_code=semantic["jsonrpc_error_code"],
                    semantic_error_type=semantic["semantic_error_type"],
                    admission_stage=semantic["admission_stage"],
                )
                LOGGER.info(
                    json.dumps(
                        {
                            "event": "public_request_outcome",
                            "request_id": str(observation.request_id),
                            "operation_id": str(observation.operation_id) if observation.operation_id else None,
                            "tenant_id": observation.tenant_id,
                            "principal_id": observation.principal_id,
                            "model_id": observation.model_id,
                            "transport": observation.transport,
                            "mcp_tool": observation.mcp_tool,
                            "http_status": observation.http_status,
                            "semantic_outcome": observation.semantic_outcome,
                            "jsonrpc_error_code": observation.jsonrpc_error_code,
                            "semantic_error_type": observation.semantic_error_type,
                            "admission_stage": observation.admission_stage,
                        },
                        separators=(",", ":"),
                    )
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
        # Rolling upgrades may add columns before all readers have upgraded.
        return [
            RequestTelemetry.model_validate({key: row[key] for key in RequestTelemetry.model_fields}) for row in rows
        ]

    async def semantic_metric_rows(self) -> list[RequestSemanticMetric]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT coalesce(model_id,'unattributed') AS model_id,transport,
                    coalesce(mcp_tool,'none') AS mcp_tool,
                    coalesce(semantic_outcome,'unknown') AS semantic_outcome,
                    coalesce(admission_stage,'unknown') AS admission_stage,count(*) AS exchanges
                FROM fs2_request_telemetry GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,4,5"""
            )
        return [RequestSemanticMetric.model_validate(dict(row)) for row in rows]

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
                            'unknown',count(*) FILTER(WHERE status_class='unknown')) AS status_classes,
                        jsonb_build_object(
                            'succeeded',count(*) FILTER(WHERE semantic_outcome='succeeded'),
                            'accepted',count(*) FILTER(WHERE semantic_outcome='accepted'),
                            'failed',count(*) FILTER(WHERE semantic_outcome='failed'),
                            'cancelled',count(*) FILTER(WHERE semantic_outcome='cancelled'),
                            'timed_out',count(*) FILTER(WHERE semantic_outcome='timed_out'),
                            'unknown',count(*) FILTER(WHERE semantic_outcome IS NULL OR semantic_outcome='unknown'))
                            AS semantic_outcomes
                    FROM observations GROUP BY 1
                ), grid AS (
                    SELECT generate_series($2::timestamptz,$3::timestamptz - interval '1 microsecond',
                        $5 * interval '1 second') AS timestamp
                ), timeline AS (
                    SELECT grid.timestamp,buckets.request_count,buckets.status_classes,buckets.semantic_outcomes
                    FROM grid LEFT JOIN buckets USING(timestamp)
                ) SELECT count(*) AS request_count,
                    count(*) FILTER (WHERE response_complete) AS completed_response_count,
                    count(*) FILTER (WHERE NOT response_complete) AS incomplete_response_count,
                    count(*) FILTER (WHERE http_status BETWEEN 200 AND 399) AS successful_http_count,
                    count(*) FILTER (WHERE http_status >= 400) AS failed_http_count,
                    count(*) FILTER (WHERE transport='mcp' AND semantic_outcome IN
                        ('failed','cancelled','timed_out')) AS mcp_tool_error_count,
                    count(*) FILTER (WHERE semantic_outcome='succeeded') AS semantic_success_count,
                    count(*) FILTER (WHERE semantic_outcome='accepted') AS semantic_accepted_count,
                    count(*) FILTER (WHERE semantic_outcome='failed') AS semantic_failed_count,
                    count(*) FILTER (WHERE semantic_outcome='cancelled') AS semantic_cancelled_count,
                    count(*) FILTER (WHERE semantic_outcome='timed_out') AS semantic_timed_out_count,
                    count(*) FILTER (WHERE semantic_outcome IS NULL OR semantic_outcome='unknown')
                        AS semantic_unknown_count,
                    count(*) FILTER (WHERE admission_stage='pre_admission' AND semantic_outcome IN
                        ('failed','cancelled','timed_out')) AS pre_admission_failure_count,
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
