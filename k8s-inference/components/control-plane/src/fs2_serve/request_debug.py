"""Opt-in HTTP/MCP debug captures, separate from usage telemetry.

Capture is off by default and, when enabled, is scoped, time-bounded and
memory-bounded. Bodies are fail closed and asymmetric. The REQUEST body (the
debugging target) is stored redacted within the store cap. A RESPONSE body is
stored only when it is a COMPLETE, valid JSON document; anything unknown,
malformed, incomplete, streaming or binary is withheld (a redaction marker). A
stored response keeps only its structure, numbers and booleans: EVERY string value
is redacted and dict entries with an unsafe key are dropped, so no arbitrary or
opaque secret in a string value or key is ever stored (and no reversible or
forgeable per-value hash is used). Response headers keep only an allowlist of safe
protocol/cache values; every other response header value is redacted. A body over
the store cap, or whose redaction expands past it, is withheld entirely rather than
stored as a boundary-cut prefix, and a response is withheld when its matching
request exceeded the cap. ``error_detail`` is a generic, payload-independent code
only; the raw exception string is never stored.

Sanitization runs off the event loop in a bounded worker pool (overload sheds the
capture). Captures are deleted by the platform's central retention purge; detail
reads require an ADMIN operator and emit an audit event; no body, header, query or
exception message reaches ordinary application logs. Model/App capture scope is
decided from server-authoritative dispatch state only, after authorization — never
a caller-declared model or tool in the request body or URL path.

Retaining response free-text — via a keyed (HMAC) non-reversible correlation marker,
an intact credential-redacted copy, or a governed on-demand reveal path — is an OPEN
owner scope decision; this module ships the safe, leak-free default (redact strings).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypeVar
from urllib.parse import unquote_plus
from uuid import UUID, uuid4

import asyncpg
from pydantic import AwareDatetime, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .crypto import Ciphertext, PayloadCipher
from .models import Principal, StrictModel
from .request_telemetry import ensure_request_id

LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")
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
        "accesskeyid",
        "secretaccesskey",
        "awsaccesskeyid",
        "awssecretaccesskey",
        "sessiontoken",
        "awssessiontoken",
        "apitoken",
        "privatekey",
        "clientkey",
    }
)
_AUTH_TOKEN = re.compile(
    rb"(?:fs2_(?:pat|admin)_[A-Za-z0-9_-]{16,}|nvapi-[A-Za-z0-9_-]{16,}|hf_[A-Za-z0-9]{16,}"
    rb"|(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ABIA|ACCA)[A-Z0-9]{16}"
    rb"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
_AUTH_SCHEME = re.compile(rb"\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_=.:-]+", re.IGNORECASE)
# Possessive quantifiers keep scalar redaction strictly linear in the input size.
# A malformed/unterminated body could otherwise force catastrophic backtracking
# and drive multi-MiB memory peaks on a bounded buffer.
_JSON_SCALAR = re.compile(rb'("(?:[^"\\]|\\.)*+")(\s*:\s*)("(?:[^"\\]|\\.)*+"|[^,}\]\s]++)')
# Single-quoted "JSON" (Python/JS repr style) never parses as JSON, so a sensitive
# value there would otherwise survive; matched only on the malformed-body path.
_JSON_SCALAR_SQ = re.compile(rb"('(?:[^'\\]|\\.)*+')(\s*:\s*)('(?:[^'\\]|\\.)*+'|[^,}\]\s]++)")
# A sensitive scalar whose value is never terminated (a truncated/malformed body
# ending mid-value). Captures the key and the partial value to end-of-buffer so
# the value can still be collected as a credential and redacted from an echo.
_JSON_UNTERMINATED = re.compile(rb'"([^"\\]{1,128})"\s*:\s*"((?:[^"\\]|\\.)*+)\Z')
# Cap on collected unterminated-value length so a huge body cannot blow up memory.
_UNTERMINATED_VALUE_MAX = 4096
# An unterminated scalar runs to end-of-buffer, so only the tail can hold one.
# Bound the search window so scanning stays linear regardless of body size.
_UNTERMINATED_SCAN = _UNTERMINATED_VALUE_MAX + 512
# RESPONSE handling is fail closed by structure, not by content type. A response is
# stored only when it is a COMPLETE, valid JSON document; anything unknown, malformed,
# incomplete, streaming or binary is withheld. Within a stored response NO string value
# is retained verbatim (any could be an opaque, non-format secret): every string value
# is redacted to a fixed non-informative marker (no reversible/forgeable hash), and
# dict entries whose key is not a safe short identifier are dropped (a key could itself
# be a secret). Numbers, booleans and null are inherently non-secret and are kept, so
# structural fields (status codes, counts) and the response shape remain for debugging.
# NOTE: retaining response free-text — via a keyed (HMAC) non-reversible correlation
# marker, an intact credential-redacted copy, or a governed on-demand reveal path — is
# an OPEN owner scope decision; this module ships the safe, leak-free default (redact).
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


def _safe_key(key: object) -> bool:
    return isinstance(key, str) and _SAFE_KEY.match(key) is not None


def _allowlist_response(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _allowlist_response(item) for key, item in value.items() if _safe_key(key)}
    if isinstance(value, list):
        return [_allowlist_response(item) for item in value]
    if isinstance(value, str):
        return REDACTED if value else value
    return value


class DebugBody(StrictModel):
    encoding: Literal["utf-8", "base64"]
    data: str
    content_type: str | None
    observed_bytes: int = Field(ge=0)
    complete: bool
    redacted: bool
    # True when the body was WITHHELD rather than stored (its `data` is a [REDACTED]
    # marker) — because it exceeded request_debug_max_body_bytes, was an
    # unknown/malformed/incomplete/binary response, or its request exceeded the cap.
    # observed_bytes still reports the full length seen on the wire. Defaults False so
    # rows captured before this field existed validate unchanged.
    truncated: bool = False


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


# Response header names whose values are safe protocol/cache metadata. Any other
# response header value is redacted (a response may set an arbitrary secret header).
_SAFE_RESPONSE_HEADERS = frozenset(
    {
        "contenttype",
        "contentlength",
        "contentencoding",
        "contentlanguage",
        "transferencoding",
        "acceptranges",
        "date",
        "age",
        "vary",
        "cachecontrol",
        "expires",
        "etag",
        "lastmodified",
        "retryafter",
        "allow",
        "connection",
        "xfs2operationid",
        "xrequestid",
        "xfs2preempted",
    }
)
_MIME_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$")


def redact_response_headers(pairs: HeaderPairs, known_credentials: Credentials = ()) -> list[tuple[str, str]]:
    """Keep only allowlisted safe response header values; redact everything else.

    A response header we do not recognize could carry an arbitrary secret, so its value
    is redacted (the name is kept for structure). Safe headers still get a known-credential
    scrub in case a credential value was echoed into one.
    """
    result = []
    for raw_name, raw_value in pairs:
        name, value = _text(raw_name), _text(raw_value)
        if _name(name) in _SAFE_RESPONSE_HEADERS and _name(name) not in _AUTH_NAMES:
            value = redact_text(value, known_credentials)
        else:
            value = REDACTED
        result.append((name, value))
    return result


def _safe_content_type(content_type: str | None) -> str | None:
    """Keep an observed Content-Type only when it looks like a MIME type; else drop it."""
    if content_type is None:
        return None
    base = content_type.split(";", 1)[0].strip()
    return content_type if _MIME_TYPE.match(base) else None


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: REDACTED if _name(key) in _AUTH_NAMES else _redact_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return value


def _try_json(raw: bytes) -> tuple[Any, bool]:
    try:
        return json.loads(raw), True
    except (ValueError, UnicodeError, RecursionError):
        return None, False


def _redact_scalar(match: re.Match[bytes]) -> bytes:
    try:
        # json.loads decodes escapes, so an escaped sensitive key still matches.
        name = json.loads(match[1])
    except (ValueError, UnicodeError):
        return match[0]
    return match[1] + match[2] + b'"[REDACTED]"' if _name(name) in _AUTH_NAMES else match[0]


def _redact_scalar_sq(match: re.Match[bytes]) -> bytes:
    """Single-quoted key/value redaction for repr-style (non-JSON) malformed bodies."""
    name = match[1][1:-1].decode("latin-1")
    return match[1] + match[2] + b"'[REDACTED]'" if _name(name) in _AUTH_NAMES else match[0]


def _redact_form(raw: bytes) -> bytes:
    """Remove sensitive values from an application/x-www-form-urlencoded body."""
    parts = []
    for part in raw.split(b"&"):
        name, separator, _ = part.partition(b"=")
        if separator and _name(unquote_plus(name.decode("latin-1"))) in _AUTH_NAMES | {"key"}:
            parts.append(name + b"=" + REDACTED.encode())
        else:
            parts.append(part)
    return b"&".join(parts)


def _is_form(content_type: str | None) -> bool:
    return content_type is not None and content_type.split(";", 1)[0].strip().lower() == (
        "application/x-www-form-urlencoded"
    )


def capture_store_limit(max_body_bytes: int | None) -> int | None:
    """Bytes to buffer for a capture: exactly the store cap.

    A body that fits in the cap is stored whole (and fully inspected); a body that
    exceeds it is withheld entirely rather than stored as a boundary-cut prefix, so
    the buffer never needs to exceed the cap.
    """
    return max_body_bytes


def suppressed_body(content_type: str | None, observed_bytes: int, complete: bool) -> DebugBody:
    """A fail-closed placeholder stored instead of a body that cannot be captured safely.

    Used when a body cannot be retained without risking a leak: it exceeded the
    store cap (an uninspected tail/boundary could hide a credential), it is a
    response whose matching request exceeded the cap (the uninspected request tail
    could be echoed), or it is an arbitrary/unstructured response body. The true
    observed length and wire-completeness are still reported; the body is withheld.
    """
    return DebugBody(
        encoding="utf-8",
        data=REDACTED,
        content_type=content_type,
        observed_bytes=observed_bytes,
        complete=complete,
        redacted=True,
        truncated=True,
    )


def _redact_prefix_runs(raw: bytes, prefixes: Credentials) -> bytes:
    """Redact a known credential PREFIX and the credential-like bytes around it.

    Used when only a prefix of a credential is known (an unterminated/bounded
    scalar). Anchoring on the high-entropy leading bytes and consuming the
    surrounding value run means redaction still fires when a response echoes the
    full value (exact-match would leave the suffix) or when only a shorter prefix
    of the secret survives the store cap (exact-match would miss it entirely).
    """
    for prefix in _known_bytes(prefixes):
        if len(prefix) < 8:
            continue
        anchor = re.escape(prefix[:8])
        pattern = rb'[^"\\\s,}\]]*' + anchor + rb'[^"\\\s,}\]]*'
        raw = re.sub(pattern, REDACTED.encode(), raw)
    return raw


def _trim_trailing_partial(raw: bytes, credentials: Credentials) -> bytes:
    """Scrub a credential whose leading bytes dangle at the end of a buffer.

    A streaming or cap boundary can cut a credential mid-value, leaving a prefix
    of it at the end of the retained bytes that whole-value redaction misses.
    """
    for credential in _known_bytes(credentials):
        for size in range(min(len(credential) - 1, len(raw)), 7, -1):
            if raw.endswith(credential[:size]):
                return raw[:-size] + REDACTED.encode()
    return raw


def _response_body(
    raw: bytes,
    content_type: str | None,
    complete: bool,
    known_credentials: tuple[bytes | str, ...],
    max_bytes: int | None,
) -> DebugBody:
    """Fail-closed response capture.

    A response is stored ONLY when it is a COMPLETE, valid JSON document. Anything
    unknown, malformed, incomplete, streaming or binary is withheld — an opaque or
    partial secret in such a body cannot be reliably scrubbed. A stored response keeps
    only its structure, numbers and booleans; EVERY string value is redacted and dict
    entries with an unsafe key are dropped (see _allowlist_response), so no arbitrary or
    opaque secret in a string value or key is ever stored. Known credentials are also
    scrubbed defensively.
    """
    observed = len(raw)
    if not complete:
        return suppressed_body(content_type, observed, complete)
    parsed, is_json = _try_json(raw)
    if not is_json:
        return suppressed_body(content_type, observed, complete)
    safe = _allowlist_response(parsed)
    body = json.dumps(safe, ensure_ascii=False, separators=(",", ":")).encode()
    body = _redact_bytes(body, known_credentials)
    if max_bytes is not None and len(body) > max_bytes:
        return suppressed_body(content_type, observed, complete)
    try:
        text = body.decode("utf-8")
        encoding: Literal["utf-8", "base64"] = "utf-8"
    except UnicodeError:
        text, encoding = base64.b64encode(body).decode("ascii"), "base64"
    return DebugBody(
        encoding=encoding,
        data=text,
        content_type=content_type,
        observed_bytes=observed,
        complete=True,
        redacted=safe != parsed or body != raw,
        truncated=False,
    )


def body_capture(
    raw: bytes,
    content_type: str | None,
    complete: bool,
    known_credentials: Credentials = (),
    max_bytes: int | None = None,
    credential_prefixes: Credentials = (),
    *,
    is_response: bool = False,
) -> DebugBody:
    observed = len(raw)
    # Withhold, never truncate. A body larger than the store cap is not stored as a
    # bounded prefix: a credential crossing the cap can leave 1..N bytes at the
    # boundary that no scrub can be trusted to remove, so the whole body is withheld
    # (fail closed). Callers still report the true observed length via the marker.
    if max_bytes is not None and observed > max_bytes:
        return suppressed_body(content_type, observed, complete)
    known_credentials = tuple(known_credentials)
    if is_response:
        # Responses are fail closed by structure (see _response_body): withhold unless a
        # complete valid JSON document, then keep only structure/numbers and redact every
        # string value. The stored Content-Type is dropped unless it looks like a MIME type.
        return _response_body(raw, _safe_content_type(content_type), complete, known_credentials, max_bytes)
    parsed, is_json = _try_json(raw)
    original = raw
    # Request sanitization is conservative and driven by key names and value formats,
    # so a sensitive value is removed on its own merits. The request body is the
    # debugging target (customer input) and is retained redacted; only responses use
    # the stricter allowlist/hash model above.
    if _is_form(content_type):
        raw = _redact_form(raw)
        parsed, is_json = _try_json(raw)
    if is_json:
        redacted_json = _redact_json(parsed)
        if redacted_json != parsed:
            raw = json.dumps(redacted_json, ensure_ascii=False, separators=(",", ":")).encode()
    else:
        # Also covers JSON inside SSE data lines and partial/malformed bodies,
        # including single-quoted (repr-style) objects that never parse as JSON.
        raw = _JSON_SCALAR.sub(_redact_scalar, raw)
        raw = _JSON_SCALAR_SQ.sub(_redact_scalar_sq, raw)
    credential_prefixes = tuple(credential_prefixes)
    raw = _redact_bytes(raw, known_credentials)
    raw = _redact_prefix_runs(raw, credential_prefixes)
    if not complete:
        # A wire-incomplete body can end mid-credential; scrub a dangling prefix.
        raw = _trim_trailing_partial(raw, (*known_credentials, *credential_prefixes))
    # Redaction can expand the body (a short value -> "[REDACTED]"). A stored body
    # is always the COMPLETE redacted body within the cap, never a clipped prefix:
    # if redaction pushed it past the cap, withhold it rather than clip a boundary.
    if max_bytes is not None and len(raw) > max_bytes:
        return suppressed_body(content_type, observed, complete)
    redacted = raw != original
    try:
        text = raw.decode("utf-8")
        encoding: Literal["utf-8", "base64"] = "utf-8"
    except UnicodeError:
        text, encoding = base64.b64encode(raw).decode("ascii"), "base64"
    return DebugBody(
        encoding=encoding,
        data=text,
        content_type=content_type,
        observed_bytes=observed,
        complete=complete,
        redacted=redacted,
        truncated=False,
    )


def bounded_body_capture(
    head: bytes,
    content_type: str | None,
    complete: bool,
    known_credentials: Credentials,
    *,
    max_bytes: int | None,
    observed_bytes: int,
    credential_prefixes: Credentials = (),
    is_response: bool = False,
) -> DebugBody:
    """Store a fully-inspected body, or withhold it when it exceeded the cap.

    ``head`` is the buffered prefix (at most the store cap) and ``observed_bytes``
    the full wire length. When the body exceeded the cap it was only partially
    buffered, so it is withheld entirely (never stored as a boundary-cut prefix);
    otherwise ``head`` holds the whole body and it is redacted and stored. ``complete``
    reports wire-completeness only. A withheld body reports ``truncated=True``.
    """
    if max_bytes is not None and observed_bytes > max_bytes:
        return suppressed_body(content_type, observed_bytes, complete)
    # observed_bytes <= max_bytes, so head holds the entire body. Pass max_bytes so
    # body_capture still withholds it if redaction expands the stored copy past the cap.
    body = body_capture(
        head, content_type, complete, known_credentials, max_bytes, credential_prefixes, is_response=is_response
    )
    return body.model_copy(update={"observed_bytes": observed_bytes})


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

        parsed, is_json = _try_json(body)
        if is_json:
            collect(parsed)
        else:
            # Non-JSON request bodies: learn sensitive values from double-quoted and
            # single-quoted scalars and form-urlencoded pairs, so a value the request
            # copy redacts is also removed from any response that echoes it.
            for match in _JSON_SCALAR.finditer(body):
                try:
                    if _name(json.loads(match[1])) in _AUTH_NAMES:
                        collect(json.loads(match[3]), True)
                except (ValueError, UnicodeError):
                    continue
            for match in _JSON_SCALAR_SQ.finditer(body):
                if _name(match[1][1:-1].decode("latin-1")) in _AUTH_NAMES:
                    scalar = match[3]
                    if scalar[:1] in (b"'", b'"'):
                        scalar = scalar[1:-1]
                    text = scalar.decode("latin-1")
                    if text and text != REDACTED:
                        values.append(text)
            for form_part in body.split(b"&"):
                form_key, form_sep, form_value = form_part.partition(b"=")
                if form_sep and _name(unquote_plus(form_key.decode("latin-1"))) in _AUTH_NAMES | {"key"}:
                    text = unquote_plus(form_value.decode("latin-1"))
                    if text and text != REDACTED:
                        values.append(text)
    return tuple(values)


def body_credential_prefixes(body: bytes | None) -> tuple[str, ...]:
    """Credential PREFIXES learned from an unterminated/bounded sensitive scalar.

    A malformed or buffer-truncated body can end in an unterminated sensitive
    value, so only its prefix is known. It is returned separately from full
    credentials so redaction removes the prefix AND any credential-like bytes
    that follow it — otherwise a response echoing the full value would keep the
    suffix. Bounded to _UNTERMINATED_VALUE_MAX so a huge body cannot blow up memory.
    """
    if not body:
        return ()
    # An unterminated scalar runs to end-of-buffer; searching only a bounded tail
    # window keeps this linear regardless of how large the body is.
    tail = _JSON_UNTERMINATED.search(body[-_UNTERMINATED_SCAN:])
    if tail is None or _name(tail[1].decode("latin-1")) not in _AUTH_NAMES:
        return ()
    partial = tail[2][:_UNTERMINATED_VALUE_MAX].decode("utf-8", "ignore")
    return (partial,) if len(partial) >= 8 and partial != REDACTED else ()


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
        # The exchange is already sanitized exactly once, off the event loop, by the
        # capturing middleware/runtime (offload_capture); the store never re-sanitizes.
        if exchange.id not in self.exchanges:
            self.exchanges[exchange.id] = exchange.model_copy(deep=True)

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
            # Already sanitized exactly once off the event loop (offload_capture); the
            # store never re-sanitizes. The model_id backfill above is a server-side
            # canonicalization from the operations table, not a re-scrub.
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


# Redaction/hashing of a near-cap body is CPU-bound and would block the asyncio event
# loop (measured ~0.2-0.8s for a 0.25-1 MiB body). It is offloaded to a worker thread,
# and a small semaphore bounds concurrency so a burst of captures cannot saturate CPU
# or spawn unbounded threads while inference requests keep flowing.
_CAPTURE_CONCURRENCY = 2
_capture_semaphore: asyncio.Semaphore | None = None


def _sanitize_semaphore() -> asyncio.Semaphore:
    global _capture_semaphore
    if _capture_semaphore is None:
        _capture_semaphore = asyncio.Semaphore(_CAPTURE_CONCURRENCY)
    return _capture_semaphore


async def offload_capture(builder: Callable[[], _T]) -> _T | None:
    """Run a CPU-bound capture builder in a worker thread with bounded concurrency.

    Sanitizing a near-cap body never blocks the event loop or inference throughput.
    Overload-withhold: if all worker slots are busy the capture is dropped entirely
    (returns None) rather than queued, shedding load so a burst can never delay
    inference — debug capture is best-effort and a dropped capture stores nothing.
    """
    semaphore = _sanitize_semaphore()
    if semaphore.locked():
        return None
    async with semaphore:
        return await asyncio.to_thread(builder)


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


_INVOKE_PATH = re.compile(r"/v1/models/([^/:]+):invoke")


def _path_model(path: str) -> str | None:
    """The model id named directly in a `/v1/models/{model}:invoke` path, if any."""
    match = _INVOKE_PATH.fullmatch(path)
    return match[1] if match else None


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _label(value: object) -> str | None:
    return value if isinstance(value, str) and value and value.isprintable() else None


@dataclass(frozen=True)
class DebugCapturePolicy:
    """Decides which exchanges may be captured.

    There is no global capture switch. Capture requires an explicit tenant and/or
    model (App) scope AND a bounded, future expiry; without either, nothing is
    captured (fail closed). A tenant allowlist matches the authenticated tenant; a
    model allowlist matches the App's model id (so pre-admission rejections for
    that App still capture). When both are set an exchange must match both. The
    default instance is disabled and captures nothing.
    """

    enabled: bool = False
    tenants: frozenset[str] = frozenset()
    models: frozenset[str] = frozenset()
    expires_at: datetime | None = None

    def should_capture(self, *, tenant_id: str | None, model_id: str | None, now: datetime) -> bool:
        if not self.enabled:
            return False
        if self.expires_at is None or now >= self.expires_at:
            return False  # A bounded, unexpired window is mandatory.
        if not self.tenants and not self.models:
            return False  # An explicit tenant or App scope is mandatory.
        tenant_ok = not self.tenants or (tenant_id is not None and tenant_id in self.tenants)
        model_ok = not self.models or (model_id is not None and model_id in self.models)
        return tenant_ok and model_ok

    def path_model_admissible(self, path_model: str | None, now: datetime) -> bool:
        """Cheap pre-buffer gate: could any exchange on this path be captured?

        Returns False when we can already prove nothing will be captured (policy
        disabled/expired/unscoped, or a model-scoped policy — with or without a
        tenant scope — whose path names a model out of scope) so the caller avoids
        buffering any bytes. A path with no named model (e.g. ``/mcp``) leaves the
        model unknown until the body is read, so it stays admissible here and is
        matched after the bounded body is available.
        """
        if not self.enabled or self.expires_at is None or now >= self.expires_at:
            return False
        if not self.tenants and not self.models:
            return False
        if self.models and path_model is not None and path_model not in self.models:
            return False
        return True

    def tenant_admissible(self, tenant_id: str | None, now: datetime) -> bool:
        """Pre-buffer gate on the authenticated tenant.

        Returns False when a tenant-scoped policy cannot admit this tenant (or the
        policy is disabled/expired/unscoped), so the public middleware can decide
        eligibility from resolved identity BEFORE buffering any bytes. A model-only
        policy has no tenant constraint, so it returns True and the model is matched
        after the bounded body is read.
        """
        if not self.enabled or self.expires_at is None or now >= self.expires_at:
            return False
        if not self.tenants and not self.models:
            return False
        if self.tenants and (tenant_id is None or tenant_id not in self.tenants):
            return False
        return True


# Fail-closed default when a middleware/client is constructed without an explicit
# policy. Production always injects a scoped policy from settings.
_DISABLED_POLICY = DebugCapturePolicy()


class DebugCaptureMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        store: DebugStore,
        persist_timeout_seconds: float = 2.0,
        principal_resolver: Callable[[str], Awaitable[Principal]] | None = None,
        max_body_bytes: int | None = None,
        policy: DebugCapturePolicy | None = None,
    ) -> None:
        self.app, self.store = app, store
        self.persist_timeout_seconds, self.principal_resolver = persist_timeout_seconds, principal_resolver
        self.max_body_bytes = max_body_bytes
        self.policy = policy or _DISABLED_POLICY

    def _store_limit(self) -> int | None:
        # Buffer the store cap plus a fixed overlap: enough to redact a credential
        # straddling the cap before truncation, but never the whole payload.
        return capture_store_limit(self.max_body_bytes)

    async def _resolve_principal(self, request_headers: list[tuple[bytes, bytes]]) -> Principal | None:
        """Best-effort read-only verify of the caller's bearer token. Never changes
        the response or assigns an unverified owner; a failure yields no principal."""
        if self.principal_resolver is None:
            return None
        authorization = next((value for key, value in request_headers if key.lower() == b"authorization"), b"")
        if not authorization.lower().startswith(b"bearer "):
            return None
        try:
            return await asyncio.wait_for(
                self.principal_resolver(authorization[7:].decode("ascii")), timeout=self.persist_timeout_seconds
            )
        except Exception:
            return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or path == "/v1/tokens"
            or path.startswith("/v1/tokens/")
            or path == "/v1/storage/credentials"
            or not (path.startswith("/v1/") or path in {"/mcp", "/mcp/"})
        ):
            await self.app(scope, receive, send)
            return
        started_at = datetime.now(UTC)
        # Gate before buffering: if the policy cannot possibly admit this path
        # (disabled/expired/unscoped, or a model-scoped policy whose path model is
        # out of scope), do not observe or accumulate any bytes.
        path_model = _path_model(path)
        if not self.policy.path_model_admissible(path_model, started_at):
            await self.app(scope, receive, send)
            return
        request_headers = list(scope.get("headers", []))
        # For a tenant-scoped policy, resolve the caller's tenant from its bearer
        # token BEFORE buffering, so an out-of-scope tenant is never observed. An
        # absent/unverifiable token has no tenant and is not admissible.
        pre_resolved: Principal | None = None
        if self.policy.tenants:
            pre_resolved = await self._resolve_principal(request_headers)
            tenant = pre_resolved.tenant_id if pre_resolved else None
            if not self.policy.tenant_admissible(tenant, started_at):
                await self.app(scope, receive, send)
                return
        state = scope.setdefault("state", {})
        request_id = ensure_request_id(scope)
        store_limit = self._store_limit()
        request_parts, response_parts = bytearray(), bytearray()
        request_observed = response_observed = 0
        request_complete = response_complete = disconnected = False
        status: int | None = None
        finished_at: datetime | None = None
        error_type: str | None = None
        response_headers: list[tuple[bytes, bytes]] = []
        response_operation: UUID | None = None
        query = scope.get("query_string", b"")

        def _accumulate(buffer: bytearray, chunk: bytes) -> None:
            # Keep only a bounded prefix; the observed counters below track the
            # true length so a large body never accumulates in memory.
            if store_limit is None:
                buffer.extend(chunk)
            elif len(buffer) < store_limit:
                buffer.extend(chunk[: store_limit - len(buffer)])

        async def observed_receive() -> Message:
            nonlocal request_complete, disconnected, request_observed
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                request_observed += len(body)
                _accumulate(request_parts, body)
                request_complete = not message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def observed_send(message: Message) -> None:
            nonlocal status, response_headers, response_operation, response_complete, finished_at, response_observed
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
                body = message.get("body", b"")
                response_observed += len(body)
                _accumulate(response_parts, body)
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
                    principal = pre_resolved  # reuse any pre-buffer resolution
                if principal is None:
                    principal = await self._resolve_principal(request_headers)
                # Model/tool attribution is server-authoritative ONLY: the trusted
                # dispatch path sets state["model_id"]/["mcp_tool"] after authorization
                # (api.py after admission; the MCP middleware after the token-policy/scope
                # checks). The URL path model and the request body are never used for
                # attribution, so a request denied before authorization is never recorded
                # under the model/tool it merely requested. (path_model still feeds the
                # cheap pre-buffer gate below, not the stored attribution.)
                model_id = _label(state.get("model_id"))
                tool = _label(state.get("mcp_tool"))
                capture_tenant = principal.tenant_id if principal else None
                # Scoped, time-bounded gate: only record exchanges the policy
                # admits. An unscoped/expired/disabled policy records nothing.
                if self.policy.should_capture(tenant_id=capture_tenant, model_id=model_id, now=datetime.now(UTC)):
                    resolved_principal = principal

                    def build_exchange() -> DebugExchange:
                        # CPU-bound (credential scan + redaction/hashing); runs in a
                        # worker thread via offload_capture so it never blocks the loop.
                        # Fail closed: a request larger than the store cap has an
                        # uninspected tail that could hold or echo a credential, so both
                        # bodies are withheld and no body credential learning is needed.
                        request_truncated = store_limit is not None and request_observed > store_limit
                        request_head = bytes(request_parts)
                        known = credential_values(
                            [*request_headers, *response_headers], query, b"" if request_truncated else request_head
                        )
                        # A credential in an unterminated/bounded request scalar is only
                        # known as a prefix; redact it (and its echoed suffix) in both bodies.
                        prefixes = () if request_truncated else body_credential_prefixes(request_head)
                        request_type = next(
                            (_text(value) for key, value in request_headers if key.lower() == b"content-type"), None
                        )
                        response_type = next(
                            (_text(value) for key, value in response_headers if key.lower() == b"content-type"), None
                        )
                        # Fail closed: if the request had an uninspected tail beyond the
                        # buffer, a credential we never saw could be echoed in the
                        # response, so the response body is withheld rather than stored.
                        response_body = (
                            suppressed_body(response_type, response_observed, response_complete)
                            if request_truncated
                            else bounded_body_capture(
                                bytes(response_parts),
                                response_type,
                                response_complete,
                                known,
                                max_bytes=self.max_body_bytes,
                                observed_bytes=response_observed,
                                credential_prefixes=prefixes,
                                is_response=True,
                            )
                        )
                        return DebugExchange(
                            id=uuid4(),
                            source="public",
                            request_id=request_id,
                            operation_id=_uuid(state.get("operation_id")) or response_operation,
                            started_at=started_at,
                            completed_at=finished_at or datetime.now(UTC),
                            tenant_id=capture_tenant,
                            principal_id=resolved_principal.principal_id if resolved_principal else None,
                            token_id=resolved_principal.token_id if resolved_principal else None,
                            model_id=model_id,
                            mcp_tool=tool,
                            endpoint=path,
                            method=str(scope.get("method", "")),
                            http_status=status,
                            error_type=error_type,
                            disconnected=disconnected,
                            query_string=redact_query(query, known),
                            request_headers=redact_headers(request_headers, known),
                            response_headers=redact_response_headers(response_headers, known),
                            request_body=bounded_body_capture(
                                bytes(request_parts),
                                request_type,
                                request_complete,
                                known,
                                max_bytes=self.max_body_bytes,
                                observed_bytes=request_observed,
                                credential_prefixes=prefixes,
                            ),
                            response_body=response_body,
                        )

                    exchange = await offload_capture(build_exchange)
                    if exchange is not None:  # None => overload-withheld (load shed)
                        await persist_debug_exchange(self.store, exchange, self.persist_timeout_seconds)
            except Exception as error:
                LOGGER.warning(
                    "request debug capture failed request_id=%s error_type=%s", request_id, type(error).__name__
                )
