"""Opt-in preproduction HTTP/MCP debug captures, separate from usage telemetry.

Model inputs and results are retained without a second payload-size ceiling.
Authentication material is removed; encrypted PostgreSQL details are available
only through the operator API. No body, header, query or exception message is
written to ordinary application logs. An unread or interrupted body is explicit.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import unquote_plus
from uuid import UUID, uuid4

import asyncpg
from pydantic import AwareDatetime, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .crypto import Ciphertext, PayloadCipher
from .models import Principal, StrictModel
from .request_telemetry import ensure_request_id

LOGGER = logging.getLogger(__name__)
REDACTED = "[REDACTED]"
Credentials = Iterable[str | bytes]
HeaderPairs = Iterable[tuple[str | bytes, str | bytes]]
_AUTH_NAMES = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "apikey",
        "xapikey",
        "xauthtoken",
        "xaccesstoken",
        "authtoken",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "clientsecret",
        "password",
        "passwd",
        "secret",
        "secretkey",
        "token",
        "credentials",
        "ngcapikey",
        "nvidiaapikey",
        "hftoken",
        "huggingfacetoken",
        "ngctoken",
        "xamzcredential",
        "xamzsignature",
        "xamzsecuritytoken",
        "signature",
        "sig",
    }
)
_AUTH_TOKEN = re.compile(
    rb"(?:fs2_(?:pat|admin)_[A-Za-z0-9_-]{16,}|nvapi-[A-Za-z0-9_-]{16,}|hf_[A-Za-z0-9]{16,}"
    rb"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
_AUTH_SCHEME = re.compile(rb"\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_=.:-]+", re.IGNORECASE)
_JSON_SCALAR = re.compile(rb'("(?:[^"\\]|\\.)*")(\s*:\s*)("(?:[^"\\]|\\.)*"|[^,}\]\s]+)')


class DebugBody(StrictModel):
    encoding: Literal["utf-8", "base64"]
    data: str
    content_type: str | None
    observed_bytes: int = Field(ge=0)
    complete: bool
    redacted: bool


class DebugMetadata(StrictModel):
    id: UUID
    source: Literal["public", "upstream"]
    request_id: UUID | None = None
    operation_id: UUID | None = None
    operation_attempt: int | None = Field(default=None, ge=0)
    upstream_attempt: int | None = Field(default=None, ge=1)
    started_at: AwareDatetime
    completed_at: AwareDatetime
    tenant_id: str | None = None
    principal_id: str | None = None
    token_id: UUID | None = None
    model_id: str | None = None
    mcp_tool: str | None = None
    endpoint: str
    method: str
    http_status: int | None = Field(default=None, ge=100, le=599)
    error_type: str | None = None
    disconnected: bool = False


class DebugExchange(DebugMetadata):
    query_string: str = ""
    request_headers: list[tuple[str, str]] = Field(default_factory=list)
    response_headers: list[tuple[str, str]] = Field(default_factory=list)
    request_body: DebugBody
    response_body: DebugBody
    error_detail: str | None = None


class DebugExchangeSummary(DebugMetadata):
    request_observed_bytes: int = Field(ge=0)
    response_observed_bytes: int = Field(ge=0)
    request_complete: bool
    response_complete: bool
    request_redacted: bool
    response_redacted: bool


class DebugExchangeList(StrictModel):
    items: list[DebugExchangeSummary]
    next_cursor: str | None = None


class DebugStore(Protocol):
    async def record(self, exchange: DebugExchange) -> None: ...

    async def list(
        self,
        *,
        model_id: str | None = None,
        operation_id: UUID | None = None,
        tenant_id: str | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> DebugExchangeList: ...

    async def get(self, exchange_id: UUID, tenant_id: str | None = None) -> DebugExchange | None: ...


def _name(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _text(value: str | bytes) -> str:
    return value.decode("latin-1") if isinstance(value, bytes) else value


def _known_bytes(known_credentials: Credentials) -> tuple[bytes, ...]:
    return tuple(
        sorted(
            {
                value if isinstance(value, bytes) else value.encode("utf-8")
                for value in known_credentials
                if value and value != REDACTED
            },
            key=len,
            reverse=True,
        )
    )


def _redact_bytes(raw: bytes, known_credentials: Credentials = ()) -> bytes:
    for credential in _known_bytes(known_credentials):
        raw = raw.replace(credential, REDACTED.encode())
    return _AUTH_SCHEME.sub(REDACTED.encode(), _AUTH_TOKEN.sub(REDACTED.encode(), raw))


def redact_text(value: str, known_credentials: Credentials = ()) -> str:
    return _redact_bytes(value.encode("utf-8"), known_credentials).decode("utf-8")


def redact_query(value: str | bytes, known_credentials: Credentials = ()) -> str:
    """Preserve ordinary query bytes/order; remove credential parameter values."""
    parts = []
    for part in _text(value).split("&"):
        name, separator, content = part.partition("=")
        if separator and _name(unquote_plus(name)) in _AUTH_NAMES | {"key"}:
            parts.append(name + "=" + REDACTED)
        else:
            decoded = unquote_plus(content)
            cleaned = redact_text(decoded, known_credentials)
            parts.append(name + separator + (content if cleaned == decoded else REDACTED))
    return "&".join(parts)


def redact_headers(pairs: HeaderPairs, known_credentials: Credentials = ()) -> list[tuple[str, str]]:
    result = []
    for raw_name, raw_value in pairs:
        name, value = _text(raw_name), _text(raw_value)
        if _name(name) in _AUTH_NAMES:
            value = REDACTED
        elif "?" in value:
            prefix, suffix = value.split("?", 1)
            value = redact_text(prefix, known_credentials) + "?" + redact_query(suffix, known_credentials)
        else:
            value = redact_text(value, known_credentials)
        result.append((name, value))
    return result


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: REDACTED if _name(key) in _AUTH_NAMES else _redact_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return value


def _redact_scalar(match: re.Match[bytes]) -> bytes:
    try:
        name = json.loads(match[1])
    except (ValueError, UnicodeError):
        return match[0]
    return match[1] + match[2] + b'"[REDACTED]"' if _name(name) in _AUTH_NAMES else match[0]


def body_capture(
    raw: bytes,
    content_type: str | None,
    complete: bool,
    known_credentials: Credentials = (),
) -> DebugBody:
    original = raw
    # Parse regardless of Content-Type: malformed/mislabeled requests are the
    # reason debug capture exists. Preserve exact original bytes when unchanged.
    try:
        parsed = json.loads(raw)
        redacted_json = _redact_json(parsed)
        if redacted_json != parsed:
            raw = json.dumps(redacted_json, ensure_ascii=False, separators=(",", ":")).encode()
    except (ValueError, UnicodeError, RecursionError):
        # Also covers JSON inside SSE data lines and partial/malformed bodies.
        raw = _JSON_SCALAR.sub(_redact_scalar, raw)
    known_credentials = tuple(known_credentials)
    raw = _redact_bytes(raw, known_credentials)
    if not complete:
        for credential in _known_bytes(known_credentials):
            for size in range(min(len(credential) - 1, len(raw)), 7, -1):
                if raw.endswith(credential[:size]):
                    raw = raw[:-size] + REDACTED.encode()
                    break
    try:
        text = raw.decode("utf-8")
        encoding: Literal["utf-8", "base64"] = "utf-8"
    except UnicodeError:
        text, encoding = base64.b64encode(raw).decode("ascii"), "base64"
    return DebugBody(
        encoding=encoding,
        data=text,
        content_type=content_type,
        observed_bytes=len(original),
        complete=complete,
        redacted=raw != original,
    )


def _body_bytes(body: DebugBody) -> bytes:
    return body.data.encode("utf-8") if body.encoding == "utf-8" else base64.b64decode(body.data, validate=True)


def credential_values(headers: HeaderPairs, query: str | bytes = "", body: bytes | None = None) -> tuple[str, ...]:
    values = []
    for raw_name, raw_value in headers:
        name, value = _text(raw_name), _text(raw_value)
        if _name(name) in _AUTH_NAMES and value and value != REDACTED:
            values.append(value)
            if _name(name) in {"authorization", "proxyauthorization"} and " " in value:
                values.append(value.split(" ", 1)[1])
            if _name(name) in {"cookie", "setcookie"}:
                values.extend(piece.split("=", 1)[1].strip() for piece in value.split(";") if "=" in piece)
    for part in _text(query).split("&"):
        key, separator, value = part.partition("=")
        if separator and _name(unquote_plus(key)) in _AUTH_NAMES | {"key"} and value:
            values.append(unquote_plus(value))
    if body:

        def collect(value: Any, credential: bool = False) -> None:
            if isinstance(value, dict):
                for name, item in value.items():
                    collect(item, credential or _name(name) in _AUTH_NAMES)
            elif isinstance(value, list):
                for item in value:
                    collect(item, credential)
            elif credential and isinstance(value, str) and value and value != REDACTED:
                values.append(value)

        try:
            collect(json.loads(body))
        except (ValueError, UnicodeError, RecursionError):
            for match in _JSON_SCALAR.finditer(body):
                try:
                    if _name(json.loads(match[1])) in _AUTH_NAMES:
                        collect(json.loads(match[3]), True)
                except (ValueError, UnicodeError):
                    continue
    return tuple(values)


def _sanitize(exchange: DebugExchange) -> DebugExchange:
    known = credential_values(
        [*exchange.request_headers, *exchange.response_headers],
        exchange.query_string,
        _body_bytes(exchange.request_body),
    )
    bodies = {}
    for field in ("request_body", "response_body"):
        previous = getattr(exchange, field)
        clean = body_capture(_body_bytes(previous), previous.content_type, previous.complete, known)
        bodies[field] = clean.model_copy(
            update={"observed_bytes": previous.observed_bytes, "redacted": previous.redacted or clean.redacted}
        )
    return exchange.model_copy(
        update={
            **bodies,
            "query_string": redact_query(exchange.query_string, known),
            "request_headers": redact_headers(exchange.request_headers, known),
            "response_headers": redact_headers(exchange.response_headers, known),
            "endpoint": redact_text(exchange.endpoint, known),
            "model_id": redact_text(exchange.model_id, known) if exchange.model_id else None,
            "mcp_tool": redact_text(exchange.mcp_tool, known) if exchange.mcp_tool else None,
            "error_detail": redact_text(exchange.error_detail, known) if exchange.error_detail is not None else None,
        }
    )


def _summary(exchange: DebugExchange) -> DebugExchangeSummary:
    return DebugExchangeSummary(
        **{field: getattr(exchange, field) for field in DebugMetadata.model_fields},
        request_observed_bytes=exchange.request_body.observed_bytes,
        response_observed_bytes=exchange.response_body.observed_bytes,
        request_complete=exchange.request_body.complete,
        response_complete=exchange.response_body.complete,
        request_redacted=exchange.request_body.redacted,
        response_redacted=exchange.response_body.redacted,
    )


def _cursor(summary: DebugExchangeSummary) -> str:
    raw = json.dumps([summary.started_at.isoformat(), str(summary.id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _pagination(limit: int, cursor: str | None) -> tuple[datetime, UUID] | None:
    if not 1 <= limit <= 200:
        raise ValueError("debug list limit must be between 1 and 200")
    if cursor is None:
        return None
    try:
        values = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(values, list) or len(values) != 2:
            raise ValueError
        at = datetime.fromisoformat(values[0])
        if at.tzinfo is None:
            raise ValueError
        return at, UUID(values[1])
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError("invalid debug pagination cursor") from error


class InMemoryDebugStore:
    def __init__(self) -> None:
        self.exchanges: dict[UUID, DebugExchange] = {}

    async def record(self, exchange: DebugExchange) -> None:
        if exchange.id not in self.exchanges:
            self.exchanges[exchange.id] = _sanitize(exchange).model_copy(deep=True)

    async def list(
        self,
        *,
        model_id: str | None = None,
        operation_id: UUID | None = None,
        tenant_id: str | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> DebugExchangeList:
        after = _pagination(limit, cursor)
        rows = sorted(
            (
                row
                for row in self.exchanges.values()
                if (model_id is None or row.model_id == model_id)
                and (operation_id is None or row.operation_id == operation_id)
                and (tenant_id is None or row.tenant_id == tenant_id)
                and (from_at is None or row.started_at >= from_at)
                and (to_at is None or row.started_at < to_at)
                and (after is None or (row.started_at, row.id) < after)
            ),
            key=lambda row: (row.started_at, row.id),
            reverse=True,
        )
        items = [_summary(row) for row in rows[:limit]]
        return DebugExchangeList(items=items, next_cursor=_cursor(items[-1]) if len(rows) > limit else None)

    async def get(self, exchange_id: UUID, tenant_id: str | None = None) -> DebugExchange | None:
        row = self.exchanges.get(exchange_id)
        return row.model_copy(deep=True) if row and (tenant_id is None or row.tenant_id == tenant_id) else None


class PostgresDebugStore:
    def __init__(self, pool: asyncpg.Pool[Any], cipher: PayloadCipher) -> None:
        self.pool, self.cipher = pool, cipher

    @staticmethod
    def _aad(exchange_id: UUID, tenant_id: str | None, model_id: str | None) -> bytes:
        return json.dumps(["fs2.debug/v1", str(exchange_id), tenant_id, model_id], separators=(",", ":")).encode()

    async def record(self, exchange: DebugExchange) -> None:
        async with self.pool.acquire() as connection:
            if exchange.model_id is None and exchange.operation_id is not None and exchange.tenant_id is not None:
                model_id = await connection.fetchval(
                    "SELECT model_id FROM fs2_operations WHERE id=$1 AND tenant_id=$2",
                    exchange.operation_id,
                    exchange.tenant_id,
                )
                exchange = exchange.model_copy(update={"model_id": model_id})
            exchange = _sanitize(exchange)
            metadata = _summary(exchange).model_dump()
            encrypted = self.cipher.encrypt(
                exchange.model_dump_json().encode(), aad=self._aad(exchange.id, exchange.tenant_id, exchange.model_id)
            )
            columns = (*DebugExchangeSummary.model_fields, "key_id", "nonce", "ciphertext")
            values = (*metadata.values(), encrypted.key_id, encrypted.nonce, encrypted.value)
            await connection.execute(
                f"INSERT INTO fs2_request_debug ({','.join(columns)}) "  # noqa: S608 - fixed model columns
                f"VALUES ({','.join(f'${index}' for index in range(1, len(columns) + 1))}) "
                "ON CONFLICT(id) DO NOTHING",
                *values,
            )

    async def list(
        self,
        *,
        model_id: str | None = None,
        operation_id: UUID | None = None,
        tenant_id: str | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> DebugExchangeList:
        after = _pagination(limit, cursor)
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                f"SELECT {','.join(DebugExchangeSummary.model_fields)} FROM fs2_request_debug "  # noqa: S608
                "WHERE ($1::text IS NULL OR model_id=$1) AND ($2::uuid IS NULL OR operation_id=$2) "
                "AND ($3::text IS NULL OR tenant_id=$3) AND ($4::timestamptz IS NULL OR started_at >= $4) "
                "AND ($5::timestamptz IS NULL OR started_at < $5) "
                "AND ($6::timestamptz IS NULL OR (started_at,id) < ($6,$7::uuid)) "
                "ORDER BY started_at DESC,id DESC LIMIT $8",
                model_id,
                operation_id,
                tenant_id,
                from_at,
                to_at,
                after[0] if after else None,
                after[1] if after else None,
                limit + 1,
            )
        items = [DebugExchangeSummary.model_validate(dict(row)) for row in rows[:limit]]
        return DebugExchangeList(items=items, next_cursor=_cursor(items[-1]) if len(rows) > limit else None)

    async def get(self, exchange_id: UUID, tenant_id: str | None = None) -> DebugExchange | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM fs2_request_debug WHERE id=$1 AND ($2::text IS NULL OR tenant_id=$2)",
                exchange_id,
                tenant_id,
            )
        if row is None:
            return None
        raw = self.cipher.decrypt(
            Ciphertext(row["key_id"], bytes(row["nonce"]), bytes(row["ciphertext"])),
            aad=self._aad(row["id"], row["tenant_id"], row["model_id"]),
        )
        return DebugExchange.model_validate_json(raw)


async def persist_debug_exchange(
    store: DebugStore,
    exchange: DebugExchange,
    persist_timeout_seconds: float = 2.0,
) -> bool:
    """Capture failure is observable but never replaces an inference response."""
    try:
        await asyncio.wait_for(store.record(exchange), timeout=persist_timeout_seconds)
        return True
    except Exception as error:
        LOGGER.warning(
            "request debug persistence failed source=%s exchange_id=%s error_type=%s",
            exchange.source,
            exchange.id,
            type(error).__name__,
        )
        return False


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _label(value: object) -> str | None:
    return value if isinstance(value, str) and value and value.isprintable() else None


class DebugCaptureMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        store: DebugStore,
        persist_timeout_seconds: float = 2.0,
        principal_resolver: Callable[[str], Awaitable[Principal]] | None = None,
    ) -> None:
        self.app, self.store = app, store
        self.persist_timeout_seconds, self.principal_resolver = persist_timeout_seconds, principal_resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or path == "/v1/tokens"
            or path.startswith("/v1/tokens/")
            or not (path.startswith("/v1/") or path in {"/mcp", "/mcp/"})
        ):
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        request_id = ensure_request_id(scope)
        started_at = datetime.now(UTC)
        request_parts, response_parts = bytearray(), bytearray()
        request_complete = response_complete = disconnected = False
        status: int | None = None
        finished_at: datetime | None = None
        error_type: str | None = None
        response_headers: list[tuple[bytes, bytes]] = []
        response_operation: UUID | None = None
        request_headers = list(scope.get("headers", []))
        query = scope.get("query_string", b"")

        async def observed_receive() -> Message:
            nonlocal request_complete, disconnected
            message = await receive()
            if message["type"] == "http.request":
                request_parts.extend(message.get("body", b""))
                request_complete = not message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def observed_send(message: Message) -> None:
            nonlocal status, response_headers, response_operation, response_complete, finished_at
            if message["type"] == "http.response.start":
                status = message["status"]
                response_headers = list(message.get("headers", []))
                response_operation = next(
                    (
                        _uuid(value.decode("ascii", errors="ignore"))
                        for key, value in response_headers
                        if key.lower() == b"x-fs2-operation-id"
                    ),
                    None,
                )
            elif message["type"] == "http.response.body":
                response_parts.extend(message.get("body", b""))
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete, finished_at = True, datetime.now(UTC)

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException as error:
            error_type = type(error).__name__
            disconnected = disconnected or isinstance(error, OSError | asyncio.CancelledError)
            raise
        finally:
            # Never drain an unread rejected body or delay it waiting for a
            # client upload. The actual observed bytes and incomplete flag are
            # more truthful than fabricating an empty/complete request.
            try:
                principal = state.get("principal")
                if not isinstance(principal, Principal):
                    principal = None
                if principal is None and self.principal_resolver is not None:
                    authorization = next(
                        (value for key, value in request_headers if key.lower() == b"authorization"), b""
                    )
                    if authorization.lower().startswith(b"bearer "):
                        try:
                            principal = await asyncio.wait_for(
                                self.principal_resolver(authorization[7:].decode("ascii")),
                                timeout=self.persist_timeout_seconds,
                            )
                        except Exception:
                            principal = None  # Never change the response or assign an unverified owner.
                model_id = _label(state.get("model_id")) or _label(scope.get("path_params", {}).get("model_id"))
                if model_id is None:
                    match = re.fullmatch(r"/v1/models/([^/:]+):invoke", path)
                    model_id = match[1] if match else None
                tool = _label(state.get("mcp_tool"))
                try:
                    claimed = json.loads(request_parts)
                    if isinstance(claimed, dict):
                        if path in {"/mcp", "/mcp/"}:
                            params = claimed.get("params", {})
                            if isinstance(params, dict):
                                tool = tool or _label(params.get("name"))
                                claimed = params.get("arguments", {})
                        if isinstance(claimed, dict):
                            model_id = model_id or _label(claimed.get("model_id", claimed.get("model")))
                except (ValueError, UnicodeError, RecursionError):
                    pass
                known = credential_values([*request_headers, *response_headers], query, bytes(request_parts))
                request_type = next(
                    (_text(value) for key, value in request_headers if key.lower() == b"content-type"), None
                )
                response_type = next(
                    (_text(value) for key, value in response_headers if key.lower() == b"content-type"), None
                )
                exchange = DebugExchange(
                    id=uuid4(),
                    source="public",
                    request_id=request_id,
                    operation_id=_uuid(state.get("operation_id")) or response_operation,
                    started_at=started_at,
                    completed_at=finished_at or datetime.now(UTC),
                    tenant_id=principal.tenant_id if principal else None,
                    principal_id=principal.principal_id if principal else None,
                    token_id=principal.token_id if principal else None,
                    model_id=model_id,
                    mcp_tool=tool,
                    endpoint=path,
                    method=str(scope.get("method", "")),
                    http_status=status,
                    error_type=error_type,
                    disconnected=disconnected,
                    query_string=redact_query(query, known),
                    request_headers=redact_headers(request_headers, known),
                    response_headers=redact_headers(response_headers, known),
                    request_body=body_capture(bytes(request_parts), request_type, request_complete, known),
                    response_body=body_capture(bytes(response_parts), response_type, response_complete, known),
                )
                await persist_debug_exchange(self.store, exchange, self.persist_timeout_seconds)
            except Exception as error:
                LOGGER.warning(
                    "request debug capture failed request_id=%s error_type=%s", request_id, type(error).__name__
                )
