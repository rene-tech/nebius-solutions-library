"""Opt-in HTTP/MCP debug captures, separate from usage telemetry.

Capture is off by default and, when enabled, is scoped, time-bounded and
memory-bounded. Bodies are fail closed and asymmetric. The REQUEST body (the
debugging target — the customer input) is stored credential-redacted within the
store cap. The RESPONSE body is NEVER stored: any part of an untrusted response can
carry an opaque secret — a string, a numeric value, an object key, or a
binary/streaming payload — so no content allowlist can be trusted. The response body
is withheld (a redaction marker); the debugging workflow is served by the captured
request plus non-sensitive typed metadata set from server dispatch state — http_status,
error_type, model/tool, timing, and a fixed MCP failure signal (``mcp_is_error`` plus a
server-origin ``mcp_failure_category`` enum and bucketed ``mcp_error_code``, so an MCP
failure inside an HTTP 200 is classifiable without the body) — never by parsing the
response bytes. Response headers keep only two structural NAMES
(``content-type`` reduced to a bare, server-known MIME type; ``content-length`` value
redacted); every other response header has BOTH its name and value redacted, so no
arbitrary header name or value is stored. A request body over the store cap, or whose
redaction expands past it, is withheld rather than stored as a boundary-cut prefix.
``error_detail`` is a generic, payload-independent code only; the raw exception string
is never stored.

Sanitization runs off the event loop in a bounded worker pool (overload sheds the
capture). Captures are deleted by the platform's central retention purge; detail
reads require an ADMIN operator and emit an audit event; no body, header, query or
exception message reaches ordinary application logs. Capture scope is decided from
server-authoritative dispatch state only, after authorization — never a caller-declared
model or tool in the request body or URL path — and a TENANT scope is mandatory so
capture can never span tenants (the model allowlist only narrows within a tenant).

Restoring response-body inspection safely — a governed on-demand audited reveal, or a
keyed (HMAC) non-reversible correlation marker — is an OPEN owner/root scope decision
layered on this safe default; see docs/request-debug-logging.md.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, TypeVar, get_args
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
# RESPONSE handling is fail closed and content-independent: the response BODY is never
# stored. Any part of an untrusted response can carry an opaque secret — not just free
# text, but a numeric value (secrets encode as digits), an object KEY (a secret used as
# a field name), or a streaming/binary payload — so no allowlist over its content can be
# trusted. The whole response body is withheld (a redaction marker); the debugging
# workflow is served by the fully captured (credential-redacted) REQUEST plus non-sensitive
# typed metadata set from server dispatch state (http_status, error_type, model/tool,
# mcp_is_error for an MCP tool-error inside an HTTP 200, timing), never by parsing the
# response bytes. Restoring response-body free-text inspection safely — a governed on-demand
# audited reveal, or a keyed (HMAC) non-reversible correlation marker — is an OPEN owner/root
# scope decision layered on
# this safe default; see docs/request-debug-logging.md.


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


# Fixed, closed enums for the MCP failure signal — the persisted fields accept ONLY these
# values (pydantic Literal validation), and the middleware filters state to these sets so no
# unrestricted/attacker-influenced string is ever stored (fail closed to None otherwise).
MCPFailureCategory = Literal[
    "invalid_request",
    "route_unavailable",
    "tool_execution_failure",
    "output_contract_failure",
    "internal_failure",
    "unknown",
]
MCPErrorCodeBucket = Literal["jsonrpc_client", "jsonrpc_server", "tool", "unknown"]
_MCP_FAILURE_CATEGORIES = frozenset(get_args(MCPFailureCategory))
_MCP_ERROR_CODE_BUCKETS = frozenset(get_args(MCPErrorCodeBucket))


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
    # Server-authoritative, non-sensitive structured signals set from dispatch state (the same
    # source request telemetry uses), never by parsing the response body, so an MCP failure
    # stays distinguishable while the body is withheld. mcp_is_error is the success-vs-tool-error
    # bool; mcp_failure_category is a FIXED server-origin enum (invalid_request / route_unavailable
    # / tool_execution_failure / output_contract_failure / internal_failure / unknown); and
    # mcp_error_code is a fixed COARSE bucket (jsonrpc_client / jsonrpc_server / tool / unknown)
    # — never a raw or verbatim code (even reserved JSON-RPC codes are bucketed), and never a raw
    # exception/message/argument/body. All three are kept on the
    # DETAIL exchange only (they ride in the encrypted payload) so they need no new clear/summary
    # column or DB migration.
    mcp_is_error: bool | None = None
    mcp_failure_category: MCPFailureCategory | None = None
    mcp_error_code: MCPErrorCodeBucket | None = None


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


# Owner-decided retention TTL for captured debug exchanges: a record older than this is
# eligible for deletion by the central retention purge; a record within it is always
# preserved. This facility NEVER deletes rows itself — the deleting purge is owned by the
# central maintenance task and is gated on the payload-free preflight below and on explicit
# rollout authorization. The constant states the agreed bound so the preflight can report it.
DEBUG_RETENTION_SECONDS = 7_776_000  # 90 days


class RetentionPreflight(StrictModel):
    """Payload-free retention snapshot, used to authorize a purge BEFORE it runs.

    Contains only aggregates over the clear ``started_at`` column — never any payload, body,
    header, or identity beyond counts and the oldest/cutoff timestamps — so it can be produced
    and reported without inspecting captured customer data. ``expired`` is the number of rows
    older than the cutoff (the only rows a purge may ever delete); ``within`` are the rows that
    must be preserved. Producing this snapshot deletes nothing.
    """

    now: AwareDatetime
    cutoff: AwareDatetime
    max_age_seconds: int = Field(ge=1)
    oldest_started_at: AwareDatetime | None
    total: int = Field(ge=0)
    expired: int = Field(ge=0)
    within: int = Field(ge=0)


class DebugStore(Protocol):
    async def record(self, exchange: DebugExchange) -> None: ...

    async def retention_preflight(self, *, now: datetime, tenant_id: str | None = None) -> RetentionPreflight:
        """Payload-free aggregate proof of retention state at the FIXED 90-day cutoff;
        deletes nothing. The cutoff is not caller-settable. ``tenant_id`` scopes the
        aggregate to one tenant (a tenant-scoped admin's own rows); None aggregates across
        all tenants (a global admin) — the caller passes its own authorized tenant so a
        tenant-scoped admin never sees cross-tenant counts."""
        ...

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


# A Content-Type is untrusted: an arbitrary subtype (e.g. ``application/OPAQUESECRET``)
# would otherwise persist a secret in the header value and in the withheld-body marker's
# content_type. Only a fixed, server-known set of bare MIME types is retained; parameters
# are always dropped and anything outside the set is omitted (fail closed). The set covers
# the media types this control plane and its upstream runtimes actually produce.
_ALLOWED_MIME = frozenset(
    {
        "application/json",
        "application/problem+json",
        "application/merge-patch+json",
        "application/apply-patch+yaml",
        "application/vnd.fs2.scientific-manifest+json",
        "application/vnd.fs2.scientific-validation+json",
        "application/octet-stream",
        "application/gzip",
        "application/zip",
        "application/x-tar",
        "application/x-nifti",
        "application/x-www-form-urlencoded",
        "text/plain",
        "text/csv",
        "text/html",
        "text/event-stream",
        "text/x-a3m",
        "text/x-fasta",
        "image/png",
        "image/jpeg",
        "image/webp",
        "chemical/x-mmcif",
        "chemical/x-pdb",
    }
)


def _safe_content_type(content_type: str | None) -> str | None:
    """Reduce a Content-Type to a bare, server-known MIME type; omit anything else.

    Parameters (``charset=…`` or an injected ``; secret=…``) are always dropped, and only a
    bare ``type/subtype`` in the fixed ``_ALLOWED_MIME`` allowlist is kept — an arbitrary or
    unknown subtype (which could smuggle a secret) is omitted (returns None). Matching is
    case-insensitive; the canonical lowercase form is stored.
    """
    if content_type is None:
        return None
    base = content_type.split(";", 1)[0].strip().lower()
    return base if base in _ALLOWED_MIME else None


def redact_response_headers(pairs: HeaderPairs) -> list[tuple[str, str]]:
    """Keep only two structural header NAMES and one typed value; redact everything else.

    A response header is untrusted in BOTH its name and its value: an arbitrary NAME (e.g.
    ``X-OPAQUESECRET``) or an arbitrary VALUE (ETag, Content-Language, X-Request-Id, or a
    numeric Content-Length whose digits encode data) can carry a secret. Only two headers are
    retained, and only when the name normalizes to a known structural header — but the stored
    name is the CANONICAL spelling, never the caller's: the normalization (``_name``) is lossy
    (it drops separators and case), so the raw name is itself an attacker channel (e.g.
    ``C_O_N_T_E_N_T_L_E_N_G_T_H`` or ``cOnTeNt.TyPe`` both normalize to a match while carrying
    bytes). ``content-type`` keeps a bare, server-known MIME value (via _safe_content_type, else
    redacted); ``content-length`` keeps its canonical name with the value ALWAYS redacted (a
    digit string can encode data; the true length is reported as observed_bytes). For every
    other header BOTH the name and value are redacted, so no arbitrary header name or value is
    ever stored; the entry is kept only to preserve the header count.
    """
    result = []
    for raw_name, raw_value in pairs:
        name, value = _text(raw_name), _text(raw_value)
        normalized = _name(name)
        if normalized == "contenttype":
            result.append(("content-type", _safe_content_type(value) or REDACTED))
        elif normalized == "contentlength":
            result.append(("content-length", REDACTED))
        else:
            result.append((REDACTED, REDACTED))
    return result


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

    The content type is reduced to a bare MIME here so no call site can leave a
    Content-Type PARAMETER (which could smuggle a secret) on a withheld body.
    """
    return DebugBody(
        encoding="utf-8",
        data=REDACTED,
        content_type=_safe_content_type(content_type),
        observed_bytes=observed_bytes,
        complete=complete,
        redacted=True,
        truncated=True,
    )


# Fixed, payload-independent marker served in place of any stored error_detail on read. Legacy rows
# may hold a raw-ish detail (an SDK could have embedded a prompt/URL/credential); the actionable
# classification is carried by error_type + http_status + the MCP failure signal, so the free-text
# detail is never disclosed on egress.
_READ_WITHHELD_DETAIL = "[detail withheld on read]"


def normalize_exchange_for_read(exchange: DebugExchange, max_body_bytes: int | None = None) -> DebugExchange:
    """Full current-contract EGRESS SANITIZER for a stored exchange, applied on EVERY serve path
    (detail read, list-derived summary via ``normalize_summary_for_read``, UI render, copy, export/
    download) WITHOUT mutating or deleting the stored row.

    The no-delete retention preserves rows for 90 days, INCLUDING legacy rows captured under an
    earlier, narrower contract. Serving those verbatim would disclose what the current contract
    withholds/redacts, so every legacy disclosure field is failed closed on the way OUT:
      - response body: ALWAYS withheld;
      - request body: withheld when wire-incomplete, a legacy stored prefix (``truncated``), or over
        the CURRENT cap; otherwise re-scrubbed with the current credential/format rules (a legacy row
        scrubbed under older, narrower rules is re-scrubbed, and one over today's cap is withheld);
      - error_detail: replaced with a fixed generic marker (never the stored free-text);
      - response headers: reduced to structural-only (canonical name, typed/redacted value);
      - request headers + query string: re-scrubbed with the current name/format rules.
    Idempotent on an already-normalized exchange. The stored ciphertext is never rewritten or deleted
    (a separately owned purge handles TTL)."""
    request = exchange.request_body
    over_cap = max_body_bytes is not None and request.observed_bytes > max_body_bytes
    if (not request.complete) or request.truncated or over_cap:
        request_body = suppressed_body(request.content_type, request.observed_bytes, request.complete)
    else:
        try:
            raw = _body_bytes(request)
        except (ValueError, UnicodeError):
            request_body = suppressed_body(request.content_type, request.observed_bytes, request.complete)
        else:
            # Re-scrub with current rules; bounded_body_capture also withholds if the whole body is
            # over the current cap or if redaction expands it past the cap.
            request_body = bounded_body_capture(
                raw,
                request.content_type,
                True,
                (),
                max_bytes=max_body_bytes,
                observed_bytes=request.observed_bytes,
            )
    response = exchange.response_body
    return exchange.model_copy(
        update={
            "request_body": request_body,
            "response_body": suppressed_body(response.content_type, response.observed_bytes, response.complete),
            "query_string": redact_query(exchange.query_string),
            "request_headers": redact_headers(exchange.request_headers),
            "response_headers": redact_response_headers(exchange.response_headers),
            "error_detail": None if exchange.error_detail is None else _READ_WITHHELD_DETAIL,
        }
    )


def normalize_summary_for_read(
    summary: DebugExchangeSummary, max_body_bytes: int | None = None
) -> DebugExchangeSummary:
    """Egress-sanitize a LIST summary's disclosure FLAGS so they match what detail-read now serves
    (a summary carries no body/header content, only booleans + observed lengths). The response is
    always withheld (redacted), and the request is redacted whenever it will be withheld on read
    (wire-incomplete or over the current cap) or was already redacted. Factual fields (observed
    lengths, wire-completeness) are unchanged."""
    return summary.model_copy(
        update={
            "response_redacted": True,
            "request_redacted": summary.request_redacted
            or (not summary.request_complete)
            or (max_body_bytes is not None and summary.request_observed_bytes > max_body_bytes),
        }
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
        # The response BODY is never stored (fail closed): any part of an untrusted
        # response — a string, a numeric value, an object key, or a binary/streaming
        # payload — can carry an opaque secret, so no content allowlist can be trusted.
        # Only the true length and wire-completeness are kept; the stored Content-Type is
        # reduced to a bare MIME type (parameters dropped) by suppressed_body, or omitted.
        return suppressed_body(content_type, observed, complete)
    if not complete:
        # Whole-or-withhold: a wire-incomplete REQUEST body is withheld ENTIRELY, never stored
        # as a partial prefix. The contract is whole-complete-within-cap or withhold — and an
        # incomplete body can also end mid-credential, so no retained prefix is trustworthy.
        # Only the true observed length and the incomplete flag are kept.
        return suppressed_body(content_type, observed, complete)
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
    # (A wire-incomplete body is already withheld above — whole-or-withhold — so the stored body
    # here is always complete; no dangling-credential trim of a partial tail is needed.)
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
    def __init__(self, *, max_body_bytes: int | None = None) -> None:
        self.exchanges: dict[UUID, DebugExchange] = {}
        # Current cap, used to withhold legacy request bodies over today's cap on the read/list path.
        self._max_body_bytes = max_body_bytes

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
        # Egress-sanitize the list flags too, so they match what detail-read now serves.
        items = [normalize_summary_for_read(_summary(row), self._max_body_bytes) for row in rows[:limit]]
        return DebugExchangeList(items=items, next_cursor=_cursor(items[-1]) if len(rows) > limit else None)

    async def get(self, exchange_id: UUID, tenant_id: str | None = None) -> DebugExchange | None:
        row = self.exchanges.get(exchange_id)
        if row is None or (tenant_id is not None and row.tenant_id != tenant_id):
            return None
        # Egress-sanitize on read: apply the current contract so a preserved (possibly legacy) row
        # never discloses a stored response/incomplete/over-cap body, raw error_detail, broad headers,
        # or under-scrubbed query/headers. Deep-copy first so the stored row is never mutated.
        return normalize_exchange_for_read(row.model_copy(deep=True), self._max_body_bytes)

    async def retention_preflight(self, *, now: datetime, tenant_id: str | None = None) -> RetentionPreflight:
        # Cutoff is fixed at the 90-day TTL, never caller-supplied. tenant_id (when set)
        # scopes the aggregate to that tenant so a tenant-scoped admin sees only its own rows.
        cutoff = now - timedelta(seconds=DEBUG_RETENTION_SECONDS)
        started = [row.started_at for row in self.exchanges.values() if tenant_id is None or row.tenant_id == tenant_id]
        expired = sum(1 for timestamp in started if timestamp < cutoff)
        return RetentionPreflight(
            now=now,
            cutoff=cutoff,
            max_age_seconds=DEBUG_RETENTION_SECONDS,
            oldest_started_at=min(started) if started else None,
            total=len(started),
            expired=expired,
            within=len(started) - expired,
        )


class PostgresDebugStore:
    def __init__(self, pool: asyncpg.Pool[Any], cipher: PayloadCipher, *, max_body_bytes: int | None = None) -> None:
        self.pool, self.cipher = pool, cipher
        # Current cap, used to withhold legacy request bodies over today's cap on the read/list path.
        self._max_body_bytes = max_body_bytes

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
        # Egress-sanitize the list flags too, so they match what detail-read now serves.
        items = [
            normalize_summary_for_read(DebugExchangeSummary.model_validate(dict(row)), self._max_body_bytes)
            for row in rows[:limit]
        ]
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
        # Egress-sanitize on read: apply the current contract so a preserved (possibly legacy) row
        # never discloses a stored response/incomplete/over-cap body, raw error_detail, broad headers,
        # or under-scrubbed query/headers. The stored ciphertext is never rewritten.
        return normalize_exchange_for_read(DebugExchange.model_validate_json(raw), self._max_body_bytes)

    async def retention_preflight(self, *, now: datetime, tenant_id: str | None = None) -> RetentionPreflight:
        # Payload-free: aggregates over the clear started_at column only. No ciphertext is
        # read or decrypted, and this SELECT deletes nothing — it is the proof produced before
        # any (separately owned, separately authorized) retention purge is allowed to run. The
        # cutoff is fixed at the 90-day TTL, never caller-supplied. tenant_id (when set) scopes
        # the aggregate to that tenant so a tenant-scoped admin never sees cross-tenant counts.
        cutoff = now - timedelta(seconds=DEBUG_RETENTION_SECONDS)
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT MIN(started_at) AS oldest, COUNT(*) AS total, "
                "COUNT(*) FILTER (WHERE started_at < $1) AS expired FROM fs2_request_debug "
                "WHERE ($2::text IS NULL OR tenant_id=$2)",
                cutoff,
                tenant_id,
            )
        total = int(row["total"]) if row is not None else 0
        expired = int(row["expired"]) if row is not None else 0
        return RetentionPreflight(
            now=now,
            cutoff=cutoff,
            max_age_seconds=DEBUG_RETENTION_SECONDS,
            oldest_started_at=row["oldest"] if row is not None else None,
            total=total,
            expired=expired,
            within=total - expired,
        )


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


class _CaptureReservation:
    """A capacity slot ISSUED by ``DebugPersistQueue.reserve``, carrying an unforgeable token.

    Won BEFORE any capture buffer is allocated and used as a context manager, so the slot is ALWAYS
    returned on exit — normal, exception, or cancellation — UNLESS it was committed to the worker via
    ``submit`` (then the worker returns it once it has persisted the capture). Ownership is enforced
    on the QUEUE side by the token, not by convention: only ``reserve`` mints a token and records it
    as live, so a directly-constructed handle (or a replayed/duplicate one) carries a token the queue
    does not recognize, and its commit/release are no-ops — capacity can never be corrupted by
    unreserved work, a fabricated handle, or a double commit/release.
    """

    def __init__(self, queue: DebugPersistQueue, token: object) -> None:
        self._queue = queue
        self._token = token

    def submit(self, builder: Callable[[], DebugExchange | None]) -> bool:
        """Commit the reserved capture to the background worker UNDER THIS reservation's token.
        Enqueue is reachable only here, and only the queue-recorded live token is accepted, so
        unreserved work can never enter the queue and the worker only ever releases the slot bound
        to THIS token. On success ownership transfers to the worker (this handle no longer releases
        it); on a (defensive) enqueue drop the slot stays with this handle and is released on context
        exit. Idempotent and non-blocking: a second submit, or one on a stale token, returns False."""
        return self._queue._commit(self._token, builder)

    def __enter__(self) -> _CaptureReservation:
        return self

    def __exit__(self, *exc: object) -> None:
        # Release the slot bound to this token unless it was committed. Idempotent on the queue side
        # (an already-released/committed/unrecognized token is a no-op), so this can never free
        # another reservation's slot even on a double exit.
        self._queue._release(self._token)


class DebugPersistQueue:
    """Bounded, non-blocking background persistence for debug captures.

    The request/failure path ONLY enqueues a build closure and returns immediately — it never
    awaits sanitization or storage, so capture cannot add latency to a customer request (public,
    MCP, or upstream). A single worker task drains the queue off-path: it sanitizes each capture
    off the event loop (offload_capture) and persists it. The queue is BOUNDED — when it is full a
    capture is DROPPED (overload shed, counted in ``dropped``), so a burst can neither block the
    request path nor grow memory without bound (each queued closure holds only a bounded buffer).
    ``drain``/``aclose`` are for TESTS and SHUTDOWN only; they must never be called on a request.

    Enqueue-time bounding (queue depth) is not enough on its own: a capture holds a cap-sized
    buffer during the WHOLE in-flight request, before the enqueue/drop decision, so arbitrarily
    many concurrent requests could pin unbounded memory ahead of the queue. A caller therefore
    wins a NON-BLOCKING reservation (``reserve``, used as a context manager) BEFORE it allocates or
    copies any buffer, and bypasses capture entirely when none is free — so total in-flight capture
    memory is bounded by the reservation count across both the building and the queued/persisting
    phases, and the slot is released on every exit path (see ``_CaptureReservation``).
    """

    def __init__(
        self,
        store: DebugStore,
        *,
        maxsize: int = 256,
        max_inflight: int | None = None,
        persist_timeout_seconds: float = 2.0,
    ) -> None:
        self._store = store
        self._queue: asyncio.Queue[tuple[Callable[[], DebugExchange | None], object]] = asyncio.Queue(
            maxsize=max(1, maxsize)
        )
        self._worker: asyncio.Task[None] | None = None
        self._persist_timeout_seconds = persist_timeout_seconds
        # Admission bound on concurrent in-flight captures: the number that may hold cap-sized
        # buffers at once, across the building phase (a request still accumulating bytes) AND the
        # queued/persisting phase. Defaults to the queue depth, so a reserved capture always fits
        # the queue (a submit drop is only a defensive backstop).
        self._max_inflight = max(1, max_inflight if max_inflight is not None else max(1, maxsize))
        # Server-side ownership registry: ONE dict token -> committed?, where each ``reserve`` mints
        # an UNFORGEABLE token (a fresh object identity) recorded with value False (reserved).
        # Committing flips that EXISTING entry's value to True IN PLACE — a non-allocating dict-value
        # update that cannot raise (no new key, no resize), so the reserved->committed transition is
        # atomic and exception-safe: there is never a window where a queued capture's token is in
        # neither state (which would undercount the bound and let it be exceeded). The handle frees a
        # still-reserved token on exit; the worker frees a committed one after persisting. A token the
        # queue does not recognize (a fabricated/directly-constructed handle, a replay, or a double
        # commit/release) is ignored, so capacity is identity-bound and released EXACTLY ONCE — never
        # by convention or a handle flag. The count is the whole registry size.
        self._slots: dict[object, bool] = {}
        self.dropped = 0

    def _inflight(self) -> int:
        """Captures currently holding a slot: every recorded token (reserved-and-building OR
        committed-and-not-yet-freed)."""
        return len(self._slots)

    def reserve(self) -> _CaptureReservation | None:
        """Non-blocking admission for ONE capture, taken BEFORE any buffer is allocated/copied.

        Mints an UNFORGEABLE token, records it server-side as reserved (value False), and returns a
        handle bound to it (use it as a context manager so the slot is released on every exit path —
        normal, exception, or cancellation — or hand it to the worker via ``handle.submit``). Returns
        None (counting a drop) at the bound, so the caller bypasses capture and allocates nothing.
        asyncio is single-threaded, so this check-mint-record runs without a lock."""
        if self._inflight() >= self._max_inflight:
            self.dropped += 1
            return None
        # Construct the handle around a fresh token FIRST, then record the token: a handle-construction
        # failure (e.g. MemoryError) must not leave a recorded-but-unheld slot. The record here is the
        # ONLY allocating registry op (a new key, pre-buffer/pre-enqueue): if it raises, nothing was
        # queued and the token was never counted, so there is no orphaned buffer and no undercount.
        token = object()
        reservation = _CaptureReservation(self, token)
        self._slots[token] = False
        return reservation

    def _commit(self, token: object, builder: Callable[[], DebugExchange | None]) -> bool:
        """Commit a reserved capture to the worker under its ISSUED token — the only enqueue path.
        Rejects (returns False, no state change) a token the queue does not currently hold as reserved
        (value False): a fabricated/directly-constructed handle, a replay, or a double commit. Enqueue
        happens FIRST; only then is the EXISTING entry flipped to True (committed) — a non-allocating,
        cannot-raise dict-value update, so there is never a window where a queued token is unrecorded
        (no undercount, no bound-exceedance, and submit surfaces no allocation error). On QueueFull (a
        defensive backstop) the token stays reserved and the handle frees it on context exit.
        Non-blocking: never blocks or awaits."""
        if self._slots.get(token) is not False:
            return False
        try:
            self._ensure_worker()
        except Exception:
            self.dropped += 1  # worker could not be (re)started; token stays reserved, freed on exit
            return False
        if self._queue.full():
            self.dropped += 1  # overload shed; token stays reserved, the handle frees it on exit
            return False
        # Commit BEFORE inserting. The queue was just observed not-full and asyncio is single-threaded
        # (no await between the check and the put), so put_nowait's internal _put WILL append the item;
        # the only way it can still raise is a post-append bookkeeping/wakeup error, by which point the
        # item is already queued. Committing first makes the worker the SOLE releaser in every path, so
        # the ownership count always matches the queue contents: inserted work can never be released
        # by context exit while it sits queued (no undercount / bound drift). The flip is a
        # non-allocating, cannot-raise update of an existing key.
        self._slots[token] = True
        try:
            self._queue.put_nowait((builder, token))
        except Exception:
            # Past the not-full guard the item is appended; keep the token committed (the worker frees
            # it after draining the item) and never re-raise onto the request path.
            LOGGER.warning("request debug enqueue raised after insert; capture will still persist")
        return True

    def _release(self, token: object) -> None:
        """Return a still-RESERVED slot to the pool, IDENTIFIED BY ITS TOKEN. Called by a handle on
        context exit. Idempotent and identity-bound: a token that is not currently reserved (value
        False) — already released, already committed (value True — the worker owns that one), or never
        issued — is a no-op, so a fabricated handle or a double exit can never free another owner's
        slot. ``del`` on the present key is non-allocating and cannot raise."""
        if self._slots.get(token) is False:
            del self._slots[token]

    def _complete(self, token: object) -> None:
        """Free a COMMITTED slot after the worker has persisted (or failed) its capture — exactly
        once, identity-bound to the token the worker dequeued. Non-allocating (pop), cannot raise."""
        self._slots.pop(token, None)

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            builder, token = await self._queue.get()
            try:
                built = await offload_capture(builder)
                if built is not None:
                    await persist_debug_exchange(self._store, built, self._persist_timeout_seconds)
            except Exception as error:
                LOGGER.warning("request debug capture failed error_type=%s", type(error).__name__)
            finally:
                # Free the slot bound to THIS capture's token (its buffers are now released), then
                # mark the queue item done for drain()/aclose().
                self._complete(token)
                self._queue.task_done()

    async def drain(self) -> None:
        """Wait for all queued captures to be processed. TESTS/SHUTDOWN ONLY — never on a request."""
        await self._queue.join()

    async def aclose(self) -> None:
        """Drain, then stop the worker. Shutdown only."""
        await self.drain()
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker


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

    There is no global capture switch. Capture requires an explicit, non-empty TENANT
    scope AND a bounded, future expiry; without a tenant scope nothing is captured (fail
    closed). A tenant scope is MANDATORY so capture can never span tenants — a model-only
    scope would otherwise capture every tenant sharing that model on a shared App. The
    model allowlist is an OPTIONAL additional narrowing WITHIN the tenant scope (it matches
    the App's model id, so pre-admission rejections for that App still capture). The default
    instance is disabled and captures nothing.
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
        if not self.tenants:
            return False  # A tenant scope is mandatory (no cross-tenant capture).
        if tenant_id is None or tenant_id not in self.tenants:
            return False
        # models is an OPTIONAL additional narrowing within the tenant scope.
        return not self.models or (model_id is not None and model_id in self.models)

    def path_model_admissible(self, path_model: str | None, now: datetime) -> bool:
        """Cheap pre-buffer gate: could any exchange on this path be captured?

        Returns False when we can already prove nothing will be captured (policy
        disabled/expired/unscoped, or a model-narrowed policy whose path names a model out
        of scope) so the caller avoids buffering any bytes. The authenticated tenant is not
        known here (the capture path never re-verifies the bearer token); the mandatory tenant
        scope is enforced after the app runs, in ``should_capture``, from the auth-resolved
        principal on scope state. A path with no named model (e.g. ``/mcp``) leaves the model
        unknown until the body is read, so it stays admissible here and is matched after the
        bounded body is available.
        """
        if not self.enabled or self.expires_at is None or now >= self.expires_at:
            return False
        if not self.tenants:
            return False
        if self.models and path_model is not None and path_model not in self.models:
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
        max_body_bytes: int | None = None,
        policy: DebugCapturePolicy | None = None,
        persist_queue: DebugPersistQueue | None = None,
    ) -> None:
        self.app, self.store = app, store
        self.persist_timeout_seconds = persist_timeout_seconds
        self.max_body_bytes = max_body_bytes
        self.policy = policy or _DISABLED_POLICY
        # Capture is persisted OFF the request path through a bounded, non-blocking queue: the
        # response is never delayed by sanitize/persist, and a burst is dropped, not queued
        # without bound. A caller may pass a shared queue (e.g. to drain it at shutdown/in tests).
        self.persist_queue = persist_queue or DebugPersistQueue(store, persist_timeout_seconds=persist_timeout_seconds)

    def _store_limit(self) -> int | None:
        # Buffer the store cap plus a fixed overlap: enough to redact a credential
        # straddling the cap before truncation, but never the whole payload.
        return capture_store_limit(self.max_body_bytes)

    async def drain(self) -> None:
        """Wait for queued captures to persist. TESTS/SHUTDOWN ONLY — never on the request path."""
        await self.persist_queue.drain()

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
        # Pre-allocation admission: win a NON-BLOCKING reservation BEFORE allocating or filling any
        # capture buffer. A path-admissible request buffers a bounded head from here on, so if no
        # reservation is free we bypass capture entirely — never allocating a buffer — rather than
        # let arbitrarily many concurrent requests each pin a cap-sized buffer ahead of the
        # enqueue/drop decision.
        reservation = self.persist_queue.reserve()
        if reservation is None:
            await self.app(scope, receive, send)
            return
        # Enter the reservation context IMMEDIATELY so the slot is released on EVERY exit path
        # (normal, exception, cancellation, or a build/submit failure) — no leak window between
        # winning the slot and protecting it; the worker takes ownership only once submit commits.
        with reservation:
            request_headers = list(scope.get("headers", []))
            # Tenant scope is enforced AFTER the app runs, from the auth-resolved principal on
            # scope state — not by re-verifying the bearer token here. Re-verifying would repeat
            # the Argon2 password hash the auth stack already performs (a per-request CPU
            # degradation), so instead a path-admissible request buffers a bounded head and, in
            # the finally below, is stored only when its resolved tenant is in scope; an
            # out-of-scope or unauthenticated request buffers the bounded head and discards it.
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
                    # Shared-principal reuse: the capture path NEVER re-verifies the bearer
                    # token. It reuses the principal the normal auth stack already resolved onto
                    # scope state (api.py sets request.state.principal after tokens.verify), so a
                    # captured request pays the Argon2 password hash exactly once (the auth path),
                    # not twice. A request with no resolved principal (unauthenticated/denied) has
                    # no tenant and is not captured (fail closed) — no cross-tenant capture.
                    candidate = state.get("principal")
                    principal = candidate if isinstance(candidate, Principal) else None
                    # Model/tool attribution is server-authoritative ONLY: the trusted
                    # dispatch path sets state["model_id"]/["mcp_tool"] after authorization
                    # (api.py after admission; the MCP middleware after the token-policy/scope
                    # checks). The URL path model and the request body are never used for
                    # attribution, so a request denied before authorization is never recorded
                    # under the model/tool it merely requested. (path_model still feeds the
                    # cheap pre-buffer gate below, not the stored attribution.)
                    model_id = _label(state.get("model_id"))
                    tool = _label(state.get("mcp_tool"))
                    # Non-sensitive structured signal set by the trusted dispatch path (the same
                    # server-authoritative source request telemetry uses), so an MCP tool error
                    # inside an HTTP 200 stays distinguishable from success while the response
                    # body is withheld. Never derived from the response bytes.
                    mcp_is_error = state.get("mcp_is_error") if isinstance(state.get("mcp_is_error"), bool) else None
                    # Fixed server-origin failure classification (enum + coarse bucket), set by the
                    # MCP dispatch path; never derived from the response bytes. Fail closed: a value
                    # is stored ONLY if it is a member of the fixed enum/bucket set, else dropped to
                    # None — so no unrestricted or attacker-influenced string can ever be persisted.
                    raw_category = state.get("mcp_failure_category")
                    mcp_failure_category = raw_category if raw_category in _MCP_FAILURE_CATEGORIES else None
                    raw_code = state.get("mcp_error_code")
                    mcp_error_code = raw_code if raw_code in _MCP_ERROR_CODE_BUCKETS else None
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
                                (_text(value) for key, value in response_headers if key.lower() == b"content-type"),
                                None,
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
                                mcp_is_error=mcp_is_error,
                                mcp_failure_category=mcp_failure_category,
                                mcp_error_code=mcp_error_code,
                                disconnected=disconnected,
                                query_string=redact_query(query, known),
                                request_headers=redact_headers(request_headers, known),
                                response_headers=redact_response_headers(response_headers),
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

                        # Enqueue for OFF-PATH persistence and return immediately — never await
                        # sanitize/persist here, so the client response is never delayed. A full
                        # bounded queue drops the capture (overload shed); the worker releases the
                        # reservation when it accepts one.
                        reservation.submit(build_exchange)
                except Exception as error:
                    LOGGER.warning(
                        "request debug capture failed request_id=%s error_type=%s", request_id, type(error).__name__
                    )
