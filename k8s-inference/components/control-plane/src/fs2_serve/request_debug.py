"""Opt-in preproduction HTTP/MCP debug captures, separate from usage telemetry.

Only a bounded redacted prefix is retained for an active signed debug session.
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
        "apitoken",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "githubtoken",
        "personalaccesstoken",
        "clientsecret",
        "password",
        "passwd",
        "secret",
        "secretkey",
        "secretaccesskey",
        "awssecretaccesskey",
        "awsaccesskeyid",
        "awssessiontoken",
        "privatekey",
        "privatekeydata",
        "deploykey",
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
        "xfs2debugauthorization",
        "xfs2debugchannelbinding",
        "signature",
        "sig",
    }
)
_AUTH_TOKEN = re.compile(
    rb"(?:fs2_(?:pat|admin)_[A-Za-z0-9_-]{16,}|nvapi-[A-Za-z0-9_-]{16,}|hf_[A-Za-z0-9]{16,}"
    rb"|gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255}"
    rb"|(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{16}"
    rb"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
_AUTH_SCHEME = re.compile(rb"\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_=.:-]+", re.IGNORECASE)
_AWS_SECRET_ASSIGNMENT = re.compile(
    rb"(?i)(\b(?:aws_)?secret_access_key\b[\s\"']*[:=][\s\"']*)([A-Za-z0-9/+=]{32,128})"
)
_PRIVATE_KEY = re.compile(
    rb"-----BEGIN ((?:[A-Z0-9]+ )?PRIVATE KEY)-----.*?-----END \1-----",
    re.DOTALL,
)
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
    debug_activation_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    debug_app_id: UUID | None = None
    debug_event_sequence: int | None = Field(default=None, ge=1)
    debug_session_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32,64}$")
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
    raw = _PRIVATE_KEY.sub(REDACTED.encode(), raw)
    raw = _AWS_SECRET_ASSIGNMENT.sub(
        lambda match: match.group(1) + REDACTED.encode(), raw
    )
    raw = _AUTH_TOKEN.sub(REDACTED.encode(), raw)
    return _AUTH_SCHEME.sub(REDACTED.encode(), raw)


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
    *,
    observed_bytes: int | None = None,
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
        observed_bytes=len(original) if observed_bytes is None else observed_bytes,
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
            "error_type": redact_text(exchange.error_type, known) if exchange.error_type else None,
            "error_detail": redact_text(exchange.error_detail, known) if exchange.error_detail is not None else None,
        }
    )


def sanitize_debug_exchange(exchange: DebugExchange) -> DebugExchange:
    """Re-apply current redaction policy before every persistence/export boundary."""

    return _sanitize(exchange)


def sanitize_debug_summary(summary: DebugExchangeSummary) -> DebugExchangeSummary:
    """Redact legacy summary columns without altering identity or accounting fields."""

    return summary.model_copy(
        update={
            "endpoint": redact_text(summary.endpoint),
            "model_id": redact_text(summary.model_id) if summary.model_id else None,
            "mcp_tool": redact_text(summary.mcp_tool) if summary.mcp_tool else None,
            "error_type": redact_text(summary.error_type) if summary.error_type else None,
        }
    )


def sanitize_debug_list(value: DebugExchangeList) -> DebugExchangeList:
    """Apply the current summary policy at every list/export boundary."""

    return value.model_copy(
        update={"items": [sanitize_debug_summary(item) for item in value.items]}
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
            self.exchanges[exchange.id] = sanitize_debug_exchange(exchange).model_copy(
                deep=True
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
        items = [sanitize_debug_summary(_summary(row)) for row in rows[:limit]]
        return DebugExchangeList(items=items, next_cursor=_cursor(items[-1]) if len(rows) > limit else None)

    async def get(self, exchange_id: UUID, tenant_id: str | None = None) -> DebugExchange | None:
        row = self.exchanges.get(exchange_id)
        return (
            sanitize_debug_exchange(row).model_copy(deep=True)
            if row and (tenant_id is None or row.tenant_id == tenant_id)
            else None
        )


class PostgresDebugStore:
    def __init__(self, pool: asyncpg.Pool[Any], cipher: PayloadCipher) -> None:
        self.pool, self.cipher = pool, cipher

    @staticmethod
    def _aad(exchange_id: UUID, tenant_id: str | None, model_id: str | None) -> bytes:
        return json.dumps(["fs2.debug/v1", str(exchange_id), tenant_id, model_id], separators=(",", ":")).encode()

    async def record(self, exchange: DebugExchange) -> None:
        async with self.pool.acquire() as connection:
            await self.record_on_connection(connection, exchange)

    async def record_on_connection(
        self, connection: asyncpg.Connection[Any], exchange: DebugExchange
    ) -> None:
        """Insert on an existing revocation-serialized transaction."""
        if exchange.model_id is None and exchange.operation_id is not None and exchange.tenant_id is not None:
            model_id = await connection.fetchval(
                "SELECT model_id FROM fs2_operations WHERE id=$1 AND tenant_id=$2",
                exchange.operation_id,
                exchange.tenant_id,
            )
            exchange = exchange.model_copy(update={"model_id": model_id})
        exchange = sanitize_debug_exchange(exchange)
        metadata = _summary(exchange).model_dump()
        encrypted = self.cipher.encrypt(
            exchange.model_dump_json().encode(),
            aad=self._aad(exchange.id, exchange.tenant_id, exchange.model_id),
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
        async with self.pool.acquire() as connection:
            return await self.list_on_connection(
                connection,
                model_id=model_id,
                operation_id=operation_id,
                tenant_id=tenant_id,
                from_at=from_at,
                to_at=to_at,
                limit=limit,
                cursor=cursor,
            )

    async def list_on_connection(
        self,
        connection: asyncpg.Connection[Any],
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
        items = [
            sanitize_debug_summary(DebugExchangeSummary.model_validate(dict(row)))
            for row in rows[:limit]
        ]
        return DebugExchangeList(items=items, next_cursor=_cursor(items[-1]) if len(rows) > limit else None)

    async def get(self, exchange_id: UUID, tenant_id: str | None = None) -> DebugExchange | None:
        async with self.pool.acquire() as connection:
            return await self.get_on_connection(connection, exchange_id, tenant_id=tenant_id)

    async def get_on_connection(
        self,
        connection: asyncpg.Connection[Any],
        exchange_id: UUID,
        tenant_id: str | None = None,
    ) -> DebugExchange | None:
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
        return sanitize_debug_exchange(DebugExchange.model_validate_json(raw))


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
        sessions: Any,
        capture_bytes: int = 1024 * 1024,
        audit_store: Any | None = None,
    ) -> None:
        self.app, self.store = app, store
        self.persist_timeout_seconds, self.principal_resolver = persist_timeout_seconds, principal_resolver
        self.sessions = sessions
        self.capture_bytes = capture_bytes
        self.audit_store = audit_store

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
        request_headers = list(scope.get("headers", []))
        initial_principal: Principal | None = None
        if self.principal_resolver is not None:
            authorization = next(
                (
                    value
                    for key, value in request_headers
                    if key.lower() == b"authorization"
                ),
                b"",
            )
            if authorization.lower().startswith(b"bearer "):
                try:
                    initial_principal = await asyncio.wait_for(
                        self.principal_resolver(authorization[7:].decode("ascii")),
                        timeout=self.persist_timeout_seconds,
                    )
                except Exception:
                    initial_principal = None
        path_model: str | None = _label(scope.get("path_params", {}).get("model_id"))
        if path_model is None:
            match = re.fullmatch(r"/v1/models/([^/:]+):invoke", path)
            path_model = match[1] if match else None
        initial_scope = None
        if initial_principal is not None and initial_principal.tenant_id is not None:
            try:
                scope_lookup = (
                    self.sessions.capture_scope(
                        initial_principal.tenant_id, path_model
                    )
                    if path_model is not None
                    else self.sessions.capture_candidate(initial_principal.tenant_id)
                )
                initial_scope = await asyncio.wait_for(
                    scope_lookup,
                    timeout=self.persist_timeout_seconds,
                )
            except Exception as error:
                LOGGER.warning(
                    "request debug activation lookup skipped request_id=%s error_type=%s",
                    request_id,
                    type(error).__name__,
                )
                initial_scope = None
        if (
            initial_scope is None
            or initial_scope["event_at"] > started_at
            or initial_scope["activation_expires_at"] <= started_at
        ):
            if self.audit_store is not None:
                try:
                    await asyncio.wait_for(
                        self.audit_store.append_audit_event(
                            actor=(
                                initial_principal.principal_id
                                if initial_principal is not None
                                else "unauthenticated"
                            ),
                            tenant_id=(
                                initial_principal.tenant_id
                                if initial_principal is not None
                                else None
                            ),
                            token_id=(
                                initial_principal.token_id
                                if initial_principal is not None
                                else None
                            ),
                            action="request.debug.capture",
                            target_type="request_debug_request",
                            target_id=str(request_id),
                            outcome="skipped",
                            detail={"reason": "no_activation_at_request_start"},
                        ),
                        timeout=self.persist_timeout_seconds,
                    )
                except Exception as error:
                    LOGGER.warning(
                        "request debug skip audit failed request_id=%s error_type=%s",
                        request_id,
                        type(error).__name__,
                    )
            await self.app(scope, receive, send)
            return
        request_parts, response_parts = bytearray(), bytearray()
        request_observed = response_observed = 0
        request_truncated = response_truncated = False
        request_complete = response_complete = disconnected = False
        status: int | None = None
        finished_at: datetime | None = None
        error_type: str | None = None
        response_headers: list[tuple[bytes, bytes]] = []
        response_operation: UUID | None = None
        query = scope.get("query_string", b"")

        async def observed_receive() -> Message:
            nonlocal request_complete, disconnected, request_observed, request_truncated
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                request_observed += len(chunk)
                remaining = max(0, self.capture_bytes - len(request_parts))
                request_parts.extend(chunk[:remaining])
                request_truncated = request_truncated or len(chunk) > remaining
                request_complete = not message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def observed_send(message: Message) -> None:
            nonlocal status, response_headers, response_operation, response_complete, finished_at
            nonlocal response_observed, response_truncated
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
                chunk = message.get("body", b"")
                response_observed += len(chunk)
                remaining = max(0, self.capture_bytes - len(response_parts))
                response_parts.extend(chunk[:remaining])
                response_truncated = response_truncated or len(chunk) > remaining
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
                    principal = initial_principal
                model_id = _label(state.get("model_id")) or path_model
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
                active_scope = initial_scope
                if (
                    principal is None
                    or initial_principal is None
                    or principal.tenant_id != initial_scope["tenant_id"]
                    or principal.principal_id != initial_principal.principal_id
                    or principal.token_id != initial_principal.token_id
                    or model_id != initial_scope["model_id"]
                ):
                    active_scope = None
                if active_scope is None:
                    if self.audit_store is not None:
                        await self.audit_store.append_audit_event(
                            actor=principal.principal_id if principal is not None else "unauthenticated",
                            tenant_id=principal.tenant_id if principal is not None else None,
                            token_id=principal.token_id if principal is not None else None,
                            action="request.debug.capture",
                            target_type="request_debug_request",
                            target_id=str(request_id),
                            outcome="skipped",
                            detail={"reason": "no_current_exact_debug_activation"},
                        )
                else:
                    known = credential_values(
                        [*request_headers, *response_headers], query, bytes(request_parts)
                    )
                    request_type = next(
                        (
                            _text(value)
                            for key, value in request_headers
                            if key.lower() == b"content-type"
                        ),
                        None,
                    )
                    response_type = next(
                        (
                            _text(value)
                            for key, value in response_headers
                            if key.lower() == b"content-type"
                        ),
                        None,
                    )
                    exchange = DebugExchange(
                        id=uuid4(),
                        source="public",
                        request_id=request_id,
                        operation_id=_uuid(state.get("operation_id")) or response_operation,
                        started_at=started_at,
                        completed_at=finished_at or datetime.now(UTC),
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        token_id=principal.token_id,
                        model_id=model_id,
                        debug_activation_sha256=str(
                            active_scope["activation_payload_sha256"]
                        ),
                        debug_app_id=active_scope["app_id"],
                        debug_event_sequence=int(active_scope["sequence"]),
                        debug_session_id=str(active_scope["session_id"]),
                        mcp_tool=tool,
                        endpoint=path,
                        method=str(scope.get("method", "")),
                        http_status=status,
                        error_type=error_type,
                        disconnected=disconnected,
                        query_string=redact_query(query, known),
                        request_headers=redact_headers(request_headers, known),
                        response_headers=redact_headers(response_headers, known),
                        request_body=body_capture(
                            bytes(request_parts),
                            request_type,
                            request_complete and not request_truncated,
                            known,
                            observed_bytes=request_observed,
                        ),
                        response_body=body_capture(
                            bytes(response_parts),
                            response_type,
                            response_complete and not response_truncated,
                            known,
                            observed_bytes=response_observed,
                        ),
                    )
                    audit = {
                        "actor": principal.principal_id,
                        "tenant_id": principal.tenant_id,
                        "token_id": principal.token_id,
                        "action": "request.debug.capture",
                        "target_type": "request_debug_exchange",
                        "target_id": str(exchange.id),
                        "outcome": "succeeded",
                        "detail": {
                            "activation_payload_sha256": str(
                                active_scope["activation_payload_sha256"]
                            ),
                            "app_id": str(active_scope["app_id"]),
                            "model_id": model_id,
                            "request_truncated": request_truncated,
                            "response_truncated": response_truncated,
                            "session_id": str(active_scope["session_id"]),
                        },
                    }
                    stored = await asyncio.wait_for(
                        self.sessions.persist_if_current(
                            store=self.store,
                            exchange=exchange,
                            expected=active_scope,
                            audit_store=self.audit_store,
                            audit=audit,
                        ),
                        timeout=self.persist_timeout_seconds,
                    )
                    if not stored and self.audit_store is not None:
                        await asyncio.wait_for(
                            self.audit_store.append_audit_event(
                                **{
                                    **audit,
                                    "outcome": "skipped",
                                    "detail": {
                                        **audit["detail"],
                                        "reason": "activation_changed_before_commit",
                                    },
                                }
                            ),
                            timeout=self.persist_timeout_seconds,
                        )
            except Exception as error:
                LOGGER.warning(
                    "request debug capture failed request_id=%s error_type=%s", request_id, type(error).__name__
                )
