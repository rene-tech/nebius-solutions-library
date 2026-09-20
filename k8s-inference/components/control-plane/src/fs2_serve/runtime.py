"""Credential-minimal, deadline-aware activation and inference HTTP adapter."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import time
import wave
import zipfile
import zlib
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from string import Formatter
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from .federation import FederationRouter, FederationTransportError
from .models import (
    ClaimedOperation,
    ModalityUsage,
    ReportedUsage,
    RuntimeIdentity,
    RuntimeLifecycleObservation,
    RuntimeResult,
)
from .registry import OperationalModel, ProbeSpec
from .request_debug import (
    DebugExchange,
    DebugStore,
    body_capture,
    credential_values,
    persist_debug_exchange,
    redact_headers,
    redact_query,
    redact_text,
)


class RuntimeOperationError(RuntimeError):
    code = "runtime_error"
    status_code = 502


class ActivationError(RuntimeOperationError):
    code = "activation_failed"
    status_code = 503


class PreemptedError(RuntimeOperationError):
    code = "runtime_preempted"
    status_code = 503


class RouteUnavailableError(RuntimeOperationError):
    code = "route_unavailable"
    status_code = 503


class RuntimeTransportError(RuntimeOperationError):
    code = "runtime_transport_error"


class RuntimeBusyError(RuntimeOperationError):
    """A qualified single-flight worker rejected work BEFORE admission."""

    code = "runtime_busy"
    status_code = 429


class RuntimeProtocolError(RuntimeOperationError):
    code = "runtime_protocol_error"


class RuntimeIdentityError(RuntimeOperationError):
    code = "runtime_identity_invalid"


_SECRET_RE = re.compile(r"(?i)(?:Bearer\s+\S+|fs2_pat_[A-Za-z0-9_-]+|https?://\S+|[A-Fa-f0-9]{64,})")
_MEDIA_TYPE_RE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
_MAX_REFLECTED_HEADER_BYTES = 256
_MAX_USAGE_FIELDS = 16
_MAX_REPORTED_TOKEN_COUNT = 2**63 - 1
_DEBUG_CAPTURE_EXTENSION = "fs2_upstream_debug_capture"
_MAX_SCIENTIFIC_ERROR_BYTES = 16 * 1024
_SAM2_MODEL_REVISION = "665f8e2ad61cf5f53d65644ff27c8ee525124610"
_SAM2_CHECKPOINT_SHA256 = "2647878d5dfa5098f2f8649825738a9345572bae2d4350a2468587ece47dd318"
_PAIDF_CHAT_MODELS = {
    "qwen3-6-27b-fp8": ("Qwen/Qwen3.6-27B-FP8", "e89b16ebf1988b3d6befa7de50abc2d76f26eb09"),
    "qwen2-5-14b-instruct": ("Qwen/Qwen2.5-14B-Instruct", "cf98f3b3bbb457ad9e2bb7baf9a0125b6b88caa8"),
}
_SCIENTIFIC_ERROR_DETAILS = {
    "evo2_memory_exhausted": (
        "Evo2 exhausted GPU memory while processing this accepted request. "
        "The operation was not automatically retried on the same runtime. "
        "The platform operator must correct runtime memory use or model capacity before replaying this shape."
    ),
    "invalid_molecule": (
        "MolMIM rejected the molecular input. Supply valid SMILES whose tokens fit "
        "the supported vocabulary and sequence window."
    ),
    "molmim_exhausted": (
        "MolMIM search exhausted: found {feasible} of {requested} distinct feasible molecules "
        "in {attempted} model decodes ({invalid} invalid; {below} below minimum similarity; "
        "{unchanged} unchanged; {duplicates} duplicate). No partial result was accepted. "
        "Review similarity and search parameters before submitting a new operation."
    ),
    "genmol_exhausted": (
        "GenMol search exhausted: accepted {accepted} of {requested} valid molecules after "
        "{attempts} sampling attempts ({sampled} sampled; {invalid} invalid; {duplicates} duplicate). "
        "No partial result was accepted. Review sampling parameters before submitting a new operation."
    ),
}
_SCIENTIFIC_ERROR_DETAIL_PATTERNS = tuple(
    re.compile("".join(
        re.escape(literal) + (r"[0-9]{1,3}" if field is not None else "")
        for literal, field, _, _ in Formatter().parse(template)
    ))
    for template in _SCIENTIFIC_ERROR_DETAILS.values()
)
_LOGGER = logging.getLogger(__name__)


class _UpstreamCapture:
    """Observe an existing dispatch without exposing its payload in public errors."""

    def __init__(
        self,
        operation: ClaimedOperation,
        endpoint: str,
        request_body: bytes,
        request_headers: dict[str, str],
        maximum: int,
        upstream_attempt: int,
    ) -> None:
        self.operation = operation
        self.endpoint = endpoint
        self.method = "POST"
        self.query_string = ""
        self.request_body = request_body
        self.request_headers = list(request_headers.items())
        self.response_headers: list[tuple[str, str]] = []
        self.request_content_type: str | None = operation.request_content_type
        self.response_content_type: str | None = None
        self.maximum = maximum
        self.upstream_attempt = upstream_attempt
        self.started_at = datetime.now(UTC)
        self.completed_at: datetime | None = None
        self.status: int | None = None
        self.content = bytearray()
        self.observed_bytes = 0
        self.read_started = False
        self.complete = False
        self.disconnected = False
        self.error_type: str | None = None
        self.error_detail: str | None = None
        self.known_credentials: list[str] = []

    def request(self, request: httpx.Request) -> None:
        self.endpoint = request.url.path
        self.query_string = request.url.query.decode("ascii", errors="replace")
        self.method = request.method
        self.request_headers = list(request.headers.multi_items())
        self.request_content_type = request.headers.get("content-type")
        try:
            self.request_body = request.content
        except httpx.RequestNotRead:
            pass  # Dispatch input is already bytes; never consume/resend a request stream.
        self.known_credentials.extend(credential_values(self.request_headers, self.query_string, self.request_body))

    def response(self, response: httpx.Response) -> None:
        self.request(response.request)
        self.status = response.status_code
        self.response_headers = list(response.headers.multi_items())
        self.response_content_type = response.headers.get("content-type")
        self.known_credentials.extend(credential_values(self.response_headers))

    def observe(self, chunk: bytes) -> None:
        self.read_started = True
        self.observed_bytes += len(chunk)
        remaining = max(0, self.maximum - len(self.content))
        self.content.extend(chunk[:remaining])
        if self.observed_bytes > self.maximum:
            self.error_type = "ResponseBodyLimitExceeded"
            self.error_detail = f"debug response capture exceeded configured maximum of {self.maximum} bytes"

    def finished(self) -> None:
        self.read_started = True
        self.complete = self.observed_bytes <= self.maximum
        self.completed_at = datetime.now(UTC)

    def failed(self, error: BaseException) -> None:
        if isinstance(error, httpx.HTTPError):
            try:
                self.request(error.request)
            except RuntimeError:
                pass  # Some transport exceptions carry no prepared request.
        self.error_type = self.error_type or type(error).__name__
        self.error_detail = self.error_detail or str(error)
        self.disconnected = self.disconnected or isinstance(error, asyncio.CancelledError)
        self.completed_at = self.completed_at or datetime.now(UTC)

    async def drain_unread(self, response: httpx.Response) -> None:
        # Success bodies are observed by the normal bounded reader. Only consume
        # a body here when normal handling did not read it (HTTP rejection,
        # preemption or invalid headers). Never resume a partially failed stream.
        if self.read_started or self.disconnected:
            return
        self.read_started = True
        try:
            async for chunk in response.aiter_bytes():
                self.observe(chunk)
                if self.observed_bytes > self.maximum:
                    return
            self.finished()
        except asyncio.CancelledError as error:
            self.failed(error)
            raise
        except Exception as error:
            # Debug-only consumption must not change an already determined
            # upstream status or replace the original protocol exception.
            self.failed(error)

    def exchange(self) -> DebugExchange:
        request = body_capture(
            self.request_body, self.request_content_type, complete=True, known_credentials=self.known_credentials
        )
        response = body_capture(
            bytes(self.content),
            self.response_content_type,
            complete=self.complete,
            known_credentials=self.known_credentials,
        )
        # observed_bytes counts bytes actually delivered by the existing decoded
        # HTTP body iterator, not wire/compressed bytes or advertised Content-Length.
        response = response.model_copy(update={"observed_bytes": self.observed_bytes})
        return DebugExchange(
            id=uuid4(),
            source="upstream",
            request_id=None,
            operation_id=self.operation.id,
            operation_attempt=self.operation.attempt,
            upstream_attempt=self.upstream_attempt,
            started_at=self.started_at,
            completed_at=self.completed_at or datetime.now(UTC),
            tenant_id=self.operation.tenant_id,
            principal_id=self.operation.principal_id,
            token_id=self.operation.token_id,
            model_id=self.operation.model_id,
            mcp_tool=None,
            endpoint=self.endpoint,
            method=self.method,
            http_status=self.status,
            error_type=self.error_type
            or ("upstream_http_error" if self.status is not None and self.status >= 400 else None),
            error_detail=redact_text(self.error_detail, known_credentials=self.known_credentials)
            if self.error_detail
            else None,
            query_string=redact_query(self.query_string, known_credentials=self.known_credentials),
            request_headers=redact_headers(self.request_headers, known_credentials=self.known_credentials),
            response_headers=redact_headers(self.response_headers, known_credentials=self.known_credentials),
            request_body=request,
            response_body=response,
            disconnected=self.disconnected,
        )


def sanitize_error_detail(value: str, limit: int = 200) -> str:
    """Return a bounded payload-independent detail for durable/public surfaces.

    Upstream exception strings are never suitable ledger data: an SDK may have
    embedded a prompt, response, URL, or credential in one. Error codes retain
    the actionable classification; the detail intentionally stays generic.
    """

    del limit
    if not value:
        return ""
    # These exact CP-owned sentences contain only static text and bounded
    # aggregate counts. Do not make arbitrary upstream messages ledger data.
    if len(value) <= 1024 and any(pattern.fullmatch(value) for pattern in _SCIENTIFIC_ERROR_DETAIL_PATTERNS):
        return value
    cleaned = _SECRET_RE.sub("[redacted]", " ".join(value.replace("\x00", "").split()))
    return "runtime operation failed" if cleaned else ""


class RuntimeMetadataProvider(Protocol):
    """Resolve allocation identity across a separate trusted control-plane boundary.

    Only opaque operation and catalog model identifiers are accepted. Implementations
    must derive Pod/node/GPU allocation from a trusted controller, proxy, or Kubernetes
    metadata source; inference response headers are never an attribution authority.
    """

    async def resolve(self, *, operation_id: UUID, model_id: str) -> RuntimeIdentity: ...


@runtime_checkable
class RuntimeLifecycleMetadataProvider(RuntimeMetadataProvider, Protocol):
    """Richer provider for exact Kubernetes/kubelet lifecycle observations."""

    async def resolve_lifecycle(
        self,
        *,
        operation_id: UUID,
        model_id: str,
    ) -> RuntimeLifecycleObservation | None: ...


@runtime_checkable
class RuntimeResponseMetadataProvider(RuntimeLifecycleMetadataProvider, Protocol):
    """Verify a response hint against the actual model Service endpoint set."""

    async def resolve_response_lifecycle(
        self, *, operation_id: UUID, model_id: str, pod_uid: str,
        service_name: str, service_namespace: str, service_port: int,
        runtime_image_digest: str, model_revision: str | None,
    ) -> RuntimeLifecycleObservation | None: ...


class NullRuntimeMetadataProvider:
    """Fail-closed default when no trusted allocation source is configured."""

    async def resolve(self, *, operation_id: UUID, model_id: str) -> RuntimeIdentity:
        del operation_id, model_id
        return RuntimeIdentity()


class RuntimeClient:
    def __init__(
        self,
        *,
        activation_timeout_seconds: float,
        runtime_timeout_seconds: float,
        max_response_bytes: int,
        client: httpx.AsyncClient | None = None,
        metadata_provider: RuntimeMetadataProvider | None = None,
        federation: FederationRouter | None = None,
        debug_store: DebugStore | None = None,
    ) -> None:
        self.activation_timeout_seconds = activation_timeout_seconds
        self.runtime_timeout_seconds = runtime_timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self._owns_client = client is None
        self.metadata_provider = metadata_provider or NullRuntimeMetadataProvider()
        self.federation = federation or FederationRouter({})
        self.debug_store = debug_store

    @asynccontextmanager
    async def _debug_stream(
        self,
        stream: AbstractAsyncContextManager[httpx.Response],
        operation: ClaimedOperation,
        endpoint: str,
        request_body: bytes,
        headers: dict[str, str],
        upstream_attempt: int,
    ) -> AsyncIterator[httpx.Response]:
        capture = _UpstreamCapture(
            operation, endpoint, request_body, headers, self.max_response_bytes, upstream_attempt
        )
        try:
            async with stream as response:
                capture.response(response)
                response.extensions[_DEBUG_CAPTURE_EXTENSION] = capture
                try:
                    yield response
                except BaseException as error:
                    capture.failed(error)
                    raise
                finally:
                    await capture.drain_unread(response)
        except BaseException as error:
            capture.failed(error)
            raise
        finally:
            if self.debug_store is not None:
                try:
                    await persist_debug_exchange(self.debug_store, capture.exchange())
                except Exception as error:
                    # Debug serialization/storage failures must not replace an
                    # inference result. Never put payloads or exception text in logs.
                    _LOGGER.warning(
                        "upstream debug capture failed operation_id=%s error_type=%s",
                        operation.id,
                        type(error).__name__,
                    )

    async def close(self) -> None:
        actions = [self.federation.close()]
        if self._owns_client:
            actions.append(self.client.aclose())
        await asyncio.gather(*actions)

    async def federation_health(self) -> dict[str, object]:
        return await self.federation.health()

    @staticmethod
    def _timeout(operation: ClaimedOperation, configured: float) -> float:
        if operation.deadline_at is None:
            return configured
        remaining = (operation.deadline_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TimeoutError("operation deadline elapsed")
        return min(configured, remaining)

    @staticmethod
    def _correlation_headers(operation: ClaimedOperation) -> dict[str, str]:
        # A model Pod can log every request header. Keep the identity mapping in
        # PostgreSQL and disclose only this random, operation-scoped correlation
        # ID plus a strictly bounded W3C trace context. Never forward tenant,
        # principal, token, Pod, node, or GPU identities.
        # Some upstream inference servers require the conventional correlation
        # header while the FS2 adapters use the namespaced form.  Both carry
        # the same random operation UUID and disclose no tenant/token identity.
        headers = {
            "x-fs2-operation-id": str(operation.id),
            # The attempt suffix keeps an explicitly retried runtime call from
            # colliding with replay-protected servers such as Evo2.
            "x-request-id": f"{operation.id}:{operation.attempt}",
        }
        traceparent = operation.traceparent
        if traceparent is not None and _TRACEPARENT_RE.fullmatch(traceparent):
            _, trace_id, parent_id, _ = traceparent.split("-")
            if trace_id != "0" * 32 and parent_id != "0" * 16:
                headers["traceparent"] = traceparent
        return headers

    async def _probe(self, model: OperationalModel, operation: ClaimedOperation, probe: ProbeSpec) -> bool:
        try:
            if model.binding.backend_class != "local-kubernetes":
                return await self.federation.probe_readiness(
                    model,
                    operation.id,
                    method=probe.method,
                    path=probe.path,
                    expected_status=probe.expected_status,
                    timeout_seconds=self._timeout(operation, probe.timeout_seconds),
                )
            async with self.client.stream(
                probe.method,
                f"{model.binding.service_origin}{probe.path}",
                timeout=self._timeout(operation, probe.timeout_seconds),
            ) as response:
                return response.status_code == probe.expected_status
        except asyncio.CancelledError:
            raise
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            httpx.StreamError,
            TimeoutError,
            UnicodeError,
            ValueError,
            TypeError,
            FederationTransportError,
        ):
            raise ActivationError("runtime readiness probe failed") from None

    async def activate(self, model: OperationalModel, operation: ClaimedOperation) -> None:
        try:
            if model.binding.backend_class != "local-kubernetes":
                healthy = await self.federation.probe_health(
                    model,
                    operation.id,
                    self._timeout(operation, self.activation_timeout_seconds),
                )
                if not healthy:
                    raise ActivationError("federated upstream health probe failed")
                deadline = time.monotonic() + self._timeout(operation, self.activation_timeout_seconds)
                interval = 0.25
                while time.monotonic() < deadline:
                    try:
                        if await self._probe(model, operation, model.readiness_probe):
                            warmup = model.warmup_probe
                            if warmup is not None and not await self._probe(model, operation, warmup):
                                raise ActivationError("runtime warmup probe failed")
                            return
                    except ActivationError:
                        pass
                    await asyncio.sleep(min(interval, self._timeout(operation, self.activation_timeout_seconds)))
                    interval = min(interval * 1.5, 5)
                raise ActivationError("federated upstream did not become ready") from None
            # Local Kubernetes mutations belong exclusively to the independent
            # activation controller. Admission reaches this point only after a
            # fenced PostgreSQL intent is READY; the data-plane client performs
            # readiness/warmup checks and dispatch, never a Kubernetes write or
            # an internal activation HTTP call.
            deadline = time.monotonic() + self._timeout(operation, self.activation_timeout_seconds)
            interval = 0.25
            while time.monotonic() < deadline:
                try:
                    if await self._probe(model, operation, model.readiness_probe):
                        warmup = model.warmup_probe
                        if warmup is not None and not await self._probe(model, operation, warmup):
                            raise ActivationError("runtime warmup probe failed")
                        return
                except ActivationError:
                    pass
                await asyncio.sleep(min(interval, self._timeout(operation, self.activation_timeout_seconds)))
                interval = min(interval * 1.5, 5)
            raise ActivationError("runtime did not become ready before activation timeout") from None
        except asyncio.CancelledError:
            raise
        except ActivationError:
            raise
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            httpx.StreamError,
            TimeoutError,
            UnicodeError,
            ValueError,
            TypeError,
            FederationTransportError,
        ):
            raise ActivationError("activation transport failed") from None

    @staticmethod
    def _header(response: httpx.Response, name: str, *, maximum: int = _MAX_REFLECTED_HEADER_BYTES) -> str | None:
        # Bind the library boundary to object first: some httpx/stub pairings
        # expose Headers.get as Any.  The explicit object/str annotations keep
        # strict mypy intact without changing coercion or whitespace cleanup.
        raw: object = response.headers.get(name)
        if raw is None:
            return None
        value: str = str(raw)
        if len(value.encode("utf-8")) > maximum or any(ord(character) < 32 for character in value):
            raise RuntimeProtocolError("runtime response header is invalid")
        return value.strip()

    async def _trusted_runtime_observation(
        self,
        operation: ClaimedOperation,
        model: OperationalModel,
        response: httpx.Response | None = None,
    ) -> tuple[RuntimeIdentity, RuntimeLifecycleObservation | None]:
        try:
            if (response is not None and model.binding.backend_class == "local-kubernetes"
                    and isinstance(self.metadata_provider, RuntimeResponseMetadataProvider)):
                names = ("x-fs2-runtime-pod-uid", "x-fs2-runtime-operation-id", "x-fs2-runtime-attempt")
                if any(name in response.headers for name in names):
                    # A malformed/stale hint is unavailable, never a reason to
                    # guess the singleton Pod or fail otherwise valid inference.
                    values = [response.headers.get_list(name) for name in names]
                    if (any(len(v) != 1 or len(v[0]) > 128 for v in values)
                            or values[1][0] != str(operation.id) or values[2][0] != str(operation.attempt)):
                        return RuntimeIdentity(), None
                    try:
                        pod_uid = str(UUID(values[0][0]))
                    except ValueError:
                        return RuntimeIdentity(), None
                    observation = await self.metadata_provider.resolve_response_lifecycle(
                        operation_id=operation.id, model_id=model.id, pod_uid=pod_uid,
                        service_name=model.binding.backend_service_name,
                        service_namespace=model.binding.backend_namespace,
                        service_port=model.binding.backend_port,
                        runtime_image_digest=model.binding.backend_runtime_image_digest,
                        model_revision=(model.dynamic_policy.publication.artifact_revision
                                        if model.dynamic_policy else model.gateway.model_revision),
                    )
                    if observation is None:
                        return RuntimeIdentity(), None
                    validated = RuntimeLifecycleObservation.model_validate(observation)
                    return validated.runtime, validated
            if isinstance(self.metadata_provider, RuntimeLifecycleMetadataProvider):
                observation = await self.metadata_provider.resolve_lifecycle(
                    operation_id=operation.id,
                    model_id=model.id,
                )
                if observation is None:
                    return RuntimeIdentity(), None
                validated = RuntimeLifecycleObservation.model_validate(observation)
                return validated.runtime, validated
            identity = await self.metadata_provider.resolve(
                operation_id=operation.id,
                model_id=model.id,
            )
            return RuntimeIdentity.model_validate(identity), None
        except asyncio.CancelledError:
            raise
        except Exception:
            # A controller/Kubernetes SDK exception may contain credentials,
            # response bodies, or cluster URLs. Never chain or persist it.
            raise RuntimeIdentityError("runtime identity is invalid") from None

    async def observe(
        self, model: OperationalModel, operation: ClaimedOperation,
    ) -> tuple[RuntimeIdentity, RuntimeLifecycleObservation | None]:
        """Reuse trusted placement/lifecycle accounting for non-HTTP transports."""
        return await self._trusted_runtime_observation(operation, model)

    @classmethod
    def _content_type(cls, response: httpx.Response, protocol: str) -> str:
        raw = cls._header(response, "content-type", maximum=128)
        if raw is None:
            raise RuntimeProtocolError("runtime content type is invalid")
        media_type = raw.split(";", 1)[0].strip().lower()
        if _MEDIA_TYPE_RE.fullmatch(media_type) is None:
            raise RuntimeProtocolError("runtime content type is invalid")
        if protocol.startswith("openai-") and media_type != "application/json" and not media_type.endswith("+json"):
            raise RuntimeProtocolError("runtime content type is invalid")
        return media_type

    @staticmethod
    def _semantic_outcome(protocol: str, body: bytes) -> str:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise RuntimeProtocolError("runtime response decoding failed") from None
        if not isinstance(payload, dict) or not payload:
            raise RuntimeProtocolError("runtime response schema is invalid")
        if protocol == "native":
            return "protocol_valid"
        if protocol in {"openai-chat", "openai-completions"}:
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise RuntimeProtocolError("runtime response schema is invalid")
            return "protocol_valid"
        if protocol in {"openai-embeddings", "openai-images"}:
            data = payload.get("data")
            if not isinstance(data, list) or not data:
                raise RuntimeProtocolError("runtime response schema is invalid")
            return "protocol_valid"
        raise RuntimeProtocolError("runtime protocol is invalid")

    @staticmethod
    def _magpie_wave_usage(body: bytes, content_type: str) -> ReportedUsage:
        """Validate the pinned local Magpie native contract, not arbitrary binary."""
        if (content_type != "audio/wav" or len(body) < 44 or body[:4] != b"RIFF"
                or body[8:12] != b"WAVE" or int.from_bytes(body[4:8], "little") + 8 != len(body)):
            raise RuntimeProtocolError("Magpie response is not a complete WAV")
        try:
            with wave.open(io.BytesIO(body), "rb") as audio:
                frames = audio.getnframes()
                if (audio.getcomptype() != "NONE" or audio.getnchannels() != 1
                        or audio.getsampwidth() != 2 or audio.getframerate() != 22050 or frames <= 0
                        or len(audio.readframes(frames + 1)) != frames * 2):
                    raise RuntimeProtocolError("Magpie WAV format or frame count is invalid")
        except (wave.Error, EOFError):
            raise RuntimeProtocolError("Magpie WAV decoding failed") from None
        return ReportedUsage(modalities=[ModalityUsage(
            modality="audio", direction="output", unit="seconds", amount=frames / 22050,
        )])

    @staticmethod
    def _visual_binary_valid(body: bytes, content_type: str) -> None:
        """Check a pinned local visual runtime container, not perceptual quality.

        Runs only after the normal bounded response read. No decoder allocation,
        external process or model-supplied identity/usage is trusted here.
        """
        def invalid() -> RuntimeProtocolError:
            return RuntimeProtocolError("visual media container is invalid")

        if content_type == "image/png":
            if not body.startswith(b"\x89PNG\r\n\x1a\n"):
                raise invalid()
            offset, chunks, image_data = 8, 0, False
            while offset < len(body):
                if len(body) - offset < 12:
                    raise invalid()
                size = int.from_bytes(body[offset:offset + 4], "big")
                end = offset + 12 + size
                kind = body[offset + 4:offset + 8]
                if end > len(body) or zlib.crc32(memoryview(body)[offset + 4:end - 4]) != int.from_bytes(
                    body[end - 4:end], "big"
                ):
                    raise invalid()
                if chunks == 0:
                    header = body[offset + 8:end - 4]
                    depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
                    if (kind != b"IHDR" or size != 13 or not int.from_bytes(header[:4], "big")
                            or not int.from_bytes(header[4:8], "big")
                            or header[8] not in depths.get(header[9], set()) or header[10:12] != b"\x00\x00"
                            or header[12] not in {0, 1}):
                        raise invalid()
                elif kind == b"IHDR":
                    raise invalid()
                if kind == b"IDAT" and size:
                    image_data = True
                if kind == b"IEND":
                    if size or end != len(body) or not image_data:
                        raise invalid()
                    return
                chunks += 1
                offset = end
            raise invalid()

        if content_type != "video/mp4":
            raise invalid()

        def boxes(start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
            while start < end:
                if end - start < 8:
                    raise invalid()
                size, header = int.from_bytes(body[start:start + 4], "big"), 8
                if size == 1:
                    if end - start < 16:
                        raise invalid()
                    size, header = int.from_bytes(body[start + 8:start + 16], "big"), 16
                elif size == 0:
                    size = end - start
                if size < header or start + size > end:
                    raise invalid()
                yield body[start + 4:start + 8], start + header, start + size
                start += size

        brands, movie, media, video = False, False, False, False
        for kind, start, end in boxes(0, len(body)):
            if not brands:
                if kind != b"ftyp" or end - start < 8 or (end - start) % 4:
                    raise invalid()
                brands = True
            elif kind == b"ftyp":
                raise invalid()
            if kind == b"mdat" and end > start:
                media = True
            if kind == b"moov":
                if movie:
                    raise invalid()
                movie = True
                for child, child_start, child_end in boxes(start, end):
                    if child != b"trak":
                        continue
                    for track, track_start, track_end in boxes(child_start, child_end):
                        if track != b"mdia":
                            continue
                        for field, field_start, field_end in boxes(track_start, track_end):
                            if field == b"hdlr" and field_end - field_start >= 12:
                                video |= body[field_start + 8:field_start + 12] == b"vide"
        if not (brands and movie and media and video):
            raise invalid()

    @staticmethod
    def _paidf_chat_valid(source_model: str, body: bytes, content_type: str, request_body: bytes) -> None:
        expected_model, revision = _PAIDF_CHAT_MODELS[source_model]
        value, request = json.loads(body), json.loads(request_body)
        if not isinstance(value, dict) or not isinstance(request, dict):
            raise RuntimeProtocolError("Reference chat envelope is invalid")
        raw, streaming = value.get("response_body"), request.get("stream", False)
        original_request = json.dumps(request, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode()
        if (content_type != "application/json" or value.get("schema") != "scientific-reference-chat/v1"
                or value.get("model") != expected_model or request.get("model") != expected_model
                or value.get("model_revision") != revision or value.get("stream") is not streaming
                or not isinstance(raw, str)
                or value.get("response_sha256") != hashlib.sha256(raw.encode()).hexdigest()
                or value.get("request_sha256") != hashlib.sha256(original_request).hexdigest()):
            raise RuntimeProtocolError("Reference chat identity or integrity is invalid")
        if not streaming:
            result = json.loads(raw)
            if (value.get("content_type") != "application/json" or not isinstance(result, dict)
                    or result.get("model") != expected_model or not isinstance(result.get("choices"), list)
                    or not result["choices"] or any(not isinstance(choice, dict)
                        or choice.get("finish_reason") is None or not isinstance(choice.get("message"), dict)
                        for choice in result["choices"])
                    or value.get("usage") != result.get("usage")):
                raise RuntimeProtocolError("Reference chat JSON is incomplete")
            return
        if value.get("content_type") != "text/event-stream":
            raise RuntimeProtocolError("Reference chat stream type is invalid")
        done, finished, usage = False, False, None
        for event in raw.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            if not data:
                continue
            if done:
                raise RuntimeProtocolError("Reference chat has data after termination")
            if data == "[DONE]":
                done = True
                continue
            token = json.loads(data)
            if (not isinstance(token, dict) or token.get("model") != expected_model
                    or not isinstance(token.get("choices"), list)
                    or any(not isinstance(choice, dict) for choice in token["choices"])):
                raise RuntimeProtocolError("Reference chat token stream is invalid")
            finished |= any(choice.get("finish_reason") is not None for choice in token["choices"])
            if token.get("usage") is not None:
                usage = token["usage"]
        if not done or not finished or value.get("usage") != usage:
            raise RuntimeProtocolError("Reference chat stream is incomplete")

    @staticmethod
    def _reported_usage(protocol: str, body: bytes, *, speech: bool = False) -> ReportedUsage | None:
        """Extract optional OpenAI token totals without making usage part of protocol validity."""

        if speech and protocol == "native":
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
                return None
            seconds = payload.get("audio_seconds") if isinstance(payload, dict) else None
            if type(seconds) not in {int, float} or not 0 < seconds <= 7200:
                return None
            return ReportedUsage(modalities=[ModalityUsage(
                modality="audio", direction="input", unit="seconds", amount=seconds,
            )])
        if protocol not in {"openai-chat", "openai-completions", "openai-embeddings"}:
            return None
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            return None
        if not isinstance(payload, dict):
            return None
        usage = payload.get("usage")
        if not isinstance(usage, dict) or len(usage) > _MAX_USAGE_FIELDS:
            return None

        def reported_count(*aliases: str) -> tuple[bool, int | None]:
            values = [usage[name] for name in aliases if name in usage]
            if not values:
                return True, None
            if any(type(value) is not int or not 0 <= value <= _MAX_REPORTED_TOKEN_COUNT for value in values):
                return False, None
            if len(set(values)) != 1:
                return False, None
            return True, values[0]

        input_valid, input_tokens = reported_count("prompt_tokens", "input_tokens")
        output_valid, output_tokens = reported_count("completion_tokens", "output_tokens")
        if not input_valid or not output_valid or (input_tokens is None and output_tokens is None):
            return None
        return ReportedUsage(input_tokens=input_tokens, output_tokens=output_tokens)

    @staticmethod
    def _scientific_error(source_model: str, status: int, body: bytes) -> tuple[str, str] | None:
        """Project known local contracts, never model-supplied messages or traces."""
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("detail"), dict):
            return None
        detail = payload["detail"]
        if source_model == "evo2-40b" and status == 500 and detail.get("code") == "MODEL_MEMORY_EXHAUSTED":
            input_length, num_tokens = detail.get("input_length"), detail.get("num_tokens")
            if (type(input_length) is int and 1 <= input_length <= 8192
                    and type(num_tokens) is int and 1 <= num_tokens <= 512
                    and detail.get("retryable") is False):
                return "model_memory_exhausted", _SCIENTIFIC_ERROR_DETAILS["evo2_memory_exhausted"]
            return None
        if source_model == "molmim" and status == 422 and detail.get("code") == "INVALID_MOLECULE":
            return "invalid_molecule", _SCIENTIFIC_ERROR_DETAILS["invalid_molecule"]

        def counts(value: object, names: tuple[str, ...], maximum: int) -> dict[str, int] | None:
            if not isinstance(value, dict):
                return None
            if any(type(value.get(name)) is not int or not 0 <= value[name] <= maximum for name in names):
                return None
            return {name: value[name] for name in names}

        if source_model == "molmim" and status == 422 and detail.get("code") == "GENERATION_EXHAUSTED":
            values = counts(detail.get("counts"), (
                "requested_molecules", "distinct_feasible_molecules", "attempted_model_decodes",
                "invalid_decodes", "below_similarity", "unchanged_decodes", "duplicate_decodes", "optimizer_steps",
            ), 512)
            if values is None:
                return None
            requested, feasible = values["requested_molecules"], values["distinct_feasible_molecules"]
            attempted = values["attempted_model_decodes"]
            if (not 1 <= requested <= 16 or feasible >= requested or not 1 <= values["optimizer_steps"] <= 16
                    or attempted < requested or sum(values[name] for name in (
                        "invalid_decodes", "below_similarity", "unchanged_decodes", "duplicate_decodes",
                        "distinct_feasible_molecules",
                    )) != attempted):
                return None
            return "generation_exhausted", _SCIENTIFIC_ERROR_DETAILS["molmim_exhausted"].format(
                feasible=feasible, requested=requested, attempted=attempted, invalid=values["invalid_decodes"],
                below=values["below_similarity"], unchanged=values["unchanged_decodes"],
                duplicates=values["duplicate_decodes"],
            )
        if source_model == "genmol" and status == 503 and detail.get("code") == "generation_exhausted":
            values = counts(detail.get("metrics"), (
                "requested_molecules", "returned_molecules", "accepted_molecules", "sampling_attempts",
                "candidate_requests", "sampled_candidates", "upstream_unreturned_candidates",
                "invalid_candidates", "duplicate_candidates", "nonfinite_score_candidates",
                "max_sampling_attempts", "max_candidate_requests",
            ), 128)
            if values is None:
                return None
            requested, accepted = values["requested_molecules"], values["accepted_molecules"]
            if (not 1 <= requested <= 16 or accepted >= requested or values["returned_molecules"] != 0
                    or not 1 <= values["sampling_attempts"] <= values["max_sampling_attempts"] <= 8
                    or not 1 <= values["candidate_requests"] <= values["max_candidate_requests"] <= requested * 8
                    or values["sampled_candidates"] + values["upstream_unreturned_candidates"]
                    != values["candidate_requests"]
                    or sum(values[name] for name in (
                        "accepted_molecules", "invalid_candidates", "duplicate_candidates",
                        "nonfinite_score_candidates",
                    )) != values["sampled_candidates"]):
                return None
            return "generation_exhausted", _SCIENTIFIC_ERROR_DETAILS["genmol_exhausted"].format(
                accepted=accepted, requested=requested, attempts=values["sampling_attempts"],
                sampled=values["sampled_candidates"], invalid=values["invalid_candidates"],
                duplicates=values["duplicate_candidates"],
            )
        return None

    async def _scientific_error_body(self, response: httpx.Response) -> bytes | None:
        """Read once within existing bounds while preserving protected debug capture.

        Only small, complete bodies may influence the public classification.
        Debug capture retains its existing larger bound; read/capture failures
        must not replace an already known upstream failure status.
        """
        content = bytearray()
        observed = 0
        capture = response.extensions.get(_DEBUG_CAPTURE_EXTENSION)
        maximum = self.max_response_bytes if isinstance(capture, _UpstreamCapture) else min(
            self.max_response_bytes, _MAX_SCIENTIFIC_ERROR_BYTES
        )
        if isinstance(capture, _UpstreamCapture):
            capture.read_started = True
        try:
            async for chunk in response.aiter_bytes():
                observed += len(chunk)
                if isinstance(capture, _UpstreamCapture):
                    capture.observe(chunk)
                content.extend(chunk[:max(0, _MAX_SCIENTIFIC_ERROR_BYTES - len(content))])
                if observed > maximum:
                    return None
            if isinstance(capture, _UpstreamCapture):
                capture.finished()
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, httpx.StreamError) as exc:
            if isinstance(capture, _UpstreamCapture):
                capture.failed(exc)
            return None
        return bytes(content) if observed <= _MAX_SCIENTIFIC_ERROR_BYTES else None

    @staticmethod
    def _scvi_multipart(request_body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes, str]]]:
        """Translate the public JSON/artifact contract to the pinned multipart runtime."""

        try:
            payload = json.loads(request_body)
            if not isinstance(payload, dict):
                raise ValueError
            encoded = payload.pop("anndata_base64")
            filename = payload.pop("filename")
            if not isinstance(encoded, str) or not isinstance(filename, str):
                raise ValueError
            content = base64.b64decode(encoded, validate=True)
        except (KeyError, ValueError, UnicodeError):
            raise RuntimeProtocolError("scVI request translation failed") from None
        if not content or len(content) > 64 * 1024 * 1024:
            raise RuntimeProtocolError("scVI AnnData input is outside the interactive bound")
        form: dict[str, str] = {}
        for key, value in payload.items():
            if value is None:
                continue
            if isinstance(value, bool):
                form[key] = "true" if value else "false"
            elif isinstance(value, str | int):
                form[key] = str(value)
            else:
                raise RuntimeProtocolError("scVI request translation failed")
        return form, {"file": (filename, content, "application/x-hdf5")}

    @staticmethod
    def _scvi_zip_valid(body: bytes, content_type: str) -> None:
        if content_type != "application/zip" or len(body) < 22 or not body.startswith(b"PK"):
            raise RuntimeProtocolError("scVI response is not a complete ZIP artifact")
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                names = archive.namelist()
                if (
                    len(names) != len(set(names))
                    or any(name.startswith("/") or ".." in name.split("/") for name in names)
                    or not {"manifest.json", "integrated.h5ad", "latent_embeddings.csv"} <= set(names)
                    or not any(name.startswith("model/") and not name.endswith("/") for name in names)
                    or archive.testzip() is not None
                ):
                    raise RuntimeProtocolError("scVI ZIP contents are invalid")
                manifest = json.loads(archive.read("manifest.json"))
        except (zipfile.BadZipFile, KeyError, ValueError, UnicodeError, RecursionError):
            raise RuntimeProtocolError("scVI ZIP contents are invalid") from None
        if (
            not isinstance(manifest, dict)
            or manifest.get("method") not in {"scvi", "scanvi"}
            or type(manifest.get("cells")) is not int
            or manifest["cells"] < 1
            or type(manifest.get("genes")) is not int
            or manifest["genes"] < 1
            or type(manifest.get("latent_dimensions")) is not int
            or manifest["latent_dimensions"] < 2
            or manifest.get("research_only") is not True
            or manifest.get("clinical_use") is not False
        ):
            raise RuntimeProtocolError("scVI result manifest is invalid")

    @staticmethod
    def _sam2_zip_valid(body: bytes, content_type: str) -> None:
        """Validate the bounded SAM 2 artifact before accepting it as a result."""

        if content_type != "application/zip" or len(body) < 22 or not body.startswith(b"PK"):
            raise RuntimeProtocolError("SAM 2 response is not a complete ZIP artifact")
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                names = archive.namelist()
                if (
                    len(names) != len(set(names))
                    or len(names) > 322
                    or any(name.startswith("/") or ".." in name.split("/") for name in names)
                    or "manifest.json" not in names
                    or archive.testzip() is not None
                ):
                    raise RuntimeProtocolError("SAM 2 ZIP contents are invalid")
                manifest = json.loads(archive.read("manifest.json"))
        except (zipfile.BadZipFile, KeyError, ValueError, UnicodeError, RecursionError):
            raise RuntimeProtocolError("SAM 2 ZIP contents are invalid") from None
        if not isinstance(manifest, dict):
            raise RuntimeProtocolError("SAM 2 result manifest is invalid")
        mode = manifest.get("mode")
        width, height = manifest.get("width"), manifest.get("height")
        objects = manifest.get("objects")
        if (
            manifest.get("schema") != "fs2.nebius.ai/sam2-result/v1"
            or manifest.get("model") != "facebook/sam2.1-hiera-large"
            or manifest.get("revision") != _SAM2_MODEL_REVISION
            or manifest.get("checkpoint_sha256") != _SAM2_CHECKPOINT_SHA256
            or mode not in {"prompted-image", "automatic-image", "prompted-video"}
            or type(width) is not int
            or type(height) is not int
            or width < 1
            or height < 1
            or width * height > 2_073_600
            or not isinstance(objects, list)
            or not 1 <= len(objects) <= 128
        ):
            raise RuntimeProtocolError("SAM 2 result manifest is invalid")
        if mode == "prompted-video":
            frame_count = manifest.get("frame_count")
            masks = [name for name in names if name.startswith("masks/") and name.endswith(".png")]
            if (
                type(frame_count) is not int
                or not 1 <= frame_count <= 320
                or len(masks) != frame_count
                or "overlay.mp4" not in names
            ):
                raise RuntimeProtocolError("SAM 2 video result is incomplete")
        elif not {"mask.png", "overlay.png"} <= set(names):
            raise RuntimeProtocolError("SAM 2 image result is incomplete")

    async def invoke(self, model: OperationalModel, operation: ClaimedOperation, request_body: bytes) -> RuntimeResult:
        try:
            endpoint = model.binding.endpoints[operation.protocol]
        except KeyError:
            raise RuntimeProtocolError("runtime protocol is invalid") from None
        source_model = (
            model.dynamic_policy.publication.source_model_ref if model.dynamic_policy is not None else model.id
        )
        headers = self._correlation_headers(operation)
        scvi = (
            model.binding.backend_class == "local-kubernetes"
            and operation.protocol == "native"
            and source_model == "scvi-scanvi"
        )
        if not scvi:
            headers["content-type"] = operation.request_content_type
        speech = (model.binding.backend_class == "local-kubernetes" and operation.protocol == "native"
                  and source_model in {
                      "nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b",
                      "parakeet-realtime-eou-120m-v1", "diar-streaming-sortformer-4spk-v2-1",
                      "magpie-tts-multilingual-357m",
                  })
        magpie = speech and source_model == "magpie-tts-multilingual-357m"
        cosmos = (model.binding.backend_class == "local-kubernetes" and operation.protocol == "native"
                  and source_model == "cosmos3-nano")
        wan2 = (model.binding.backend_class == "local-kubernetes" and operation.protocol == "native"
                and source_model in {"wan2-2-t2v-nim", "wan2-2-i2v-nim"})
        cosmos_transfer = (model.binding.backend_class == "local-kubernetes" and operation.protocol == "native"
                           and source_model == "cosmos-transfer2-5-2b")
        sam2 = (model.binding.backend_class == "local-kubernetes" and operation.protocol == "native"
                and source_model == "sam2-1-hiera-large")
        paidf_chat = (model.binding.backend_class == "local-kubernetes" and operation.protocol == "native"
                      and source_model in _PAIDF_CHAT_MODELS)
        if speech or paidf_chat:
            # A retry after explicit pre-admission busy must be able to select
            # another Service endpoint instead of sticking to a busy socket.
            headers["connection"] = "close"
        started = time.monotonic()
        try:
            if model.binding.backend_class == "local-kubernetes":
                request_arguments: dict[str, Any]
                if scvi:
                    form, files = self._scvi_multipart(request_body)
                    request_arguments = {"data": form, "files": files}
                else:
                    request_arguments = {"content": request_body}
                stream = self.client.stream(
                    "POST",
                    f"{model.binding.service_origin}{endpoint}",
                    headers=headers,
                    timeout=self._timeout(operation, self.runtime_timeout_seconds),
                    **request_arguments,
                )
                if self.debug_store is not None:
                    stream = self._debug_stream(stream, operation, endpoint, request_body, headers, 1)
            else:
                stream = self.federation.stream(
                    model,
                    operation_id=operation.id,
                    method="POST",
                    path=endpoint,
                    timeout_seconds=self._timeout(operation, self.runtime_timeout_seconds),
                    content_type=operation.request_content_type,
                    content=request_body,
                    exchange_observer=(
                        lambda context, attempt: self._debug_stream(
                            context, operation, endpoint, request_body, headers, attempt
                        )
                    )
                    if self.debug_store is not None
                    else None,
                )
            async with stream as response:
                content_type = self._content_type(response, operation.protocol)
                preempted = self._header(response, "x-fs2-preempted", maximum=16)
                if preempted is not None and preempted.lower() not in {"true", "false"}:
                    raise RuntimeProtocolError("runtime response header is invalid")
                if response.status_code in (409, 410) and preempted is not None and preempted.lower() == "true":
                    raise PreemptedError("runtime reported preemption")
                if not response.is_success:
                    scientific_error = None
                    if (model.binding.backend_class == "local-kubernetes" and operation.protocol == "native"
                            and source_model in {"molmim", "genmol", "evo2-40b"}
                            and response.status_code in {422, 500, 503}
                            and content_type == "application/json"):
                        rejected_body = await self._scientific_error_body(response)
                        if rejected_body is not None:
                            scientific_error = self._scientific_error(source_model, response.status_code, rejected_body)
                    if (speech or paidf_chat) and response.status_code == 429:
                        rejected = bytearray()
                        capture = response.extensions.get(_DEBUG_CAPTURE_EXTENSION)
                        if isinstance(capture, _UpstreamCapture):
                            capture.read_started = True
                        async for chunk in response.aiter_bytes():
                            if isinstance(capture, _UpstreamCapture):
                                capture.observe(chunk)
                            rejected.extend(chunk)
                            if len(rejected) > 4096:
                                raise RuntimeProtocolError("worker busy response exceeds limit")
                        if isinstance(capture, _UpstreamCapture):
                            capture.finished()
                        try:
                            busy = json.loads(rejected) == {"detail": "runtime_busy"}
                        except (ValueError, UnicodeError):
                            busy = False
                        if busy:
                            raise RuntimeBusyError("worker capacity is occupied")
                    # Public failures carry only CP-owned wording and validated
                    # aggregate counts for the recognized scientific contracts.
                    # The optional encrypted debug capture owns original bodies.
                    runtime, lifecycle = await self._trusted_runtime_observation(operation, model, response)
                    return RuntimeResult(
                        status_code=response.status_code,
                        body=b"",
                        content_type=content_type,
                        elapsed_seconds=time.monotonic() - started,
                        runtime=runtime,
                        semantic_outcome="not_evaluated",
                        failure_code=scientific_error[0] if scientific_error else "upstream_http_error",
                        failure_detail=scientific_error[1] if scientific_error else None,
                        lifecycle=lifecycle,
                    )
                content = bytearray()
                capture = response.extensions.get(_DEBUG_CAPTURE_EXTENSION)
                if isinstance(capture, _UpstreamCapture):
                    capture.read_started = True
                async for chunk in response.aiter_bytes():
                    if isinstance(capture, _UpstreamCapture):
                        capture.observe(chunk)
                    content.extend(chunk)
                    if len(content) > self.max_response_bytes:
                        raise RuntimeProtocolError("runtime response exceeded configured maximum")
                if isinstance(capture, _UpstreamCapture):
                    capture.finished()
                if magpie:
                    usage = self._magpie_wave_usage(bytes(content), content_type)
                    semantic = "protocol_valid"
                elif cosmos_transfer:
                    if content_type != "video/mp4":
                        raise RuntimeProtocolError("Cosmos Transfer must return a verified MP4")
                    self._visual_binary_valid(bytes(content), content_type)
                    semantic, usage = "protocol_valid", None
                elif ((cosmos and content_type == "image/png")
                      or ((cosmos or wan2) and content_type == "video/mp4")):
                    self._visual_binary_valid(bytes(content), content_type)
                    semantic, usage = "protocol_valid", None
                elif scvi:
                    self._scvi_zip_valid(bytes(content), content_type)
                    semantic, usage = "protocol_valid", None
                elif sam2:
                    self._sam2_zip_valid(bytes(content), content_type)
                    semantic, usage = "protocol_valid", None
                elif paidf_chat:
                    self._paidf_chat_valid(source_model, bytes(content), content_type, request_body)
                    semantic = "protocol_valid"
                    usage = self._reported_usage("openai-chat", bytes(content))
                else:
                    semantic = self._semantic_outcome(operation.protocol, bytes(content))
                    usage = self._reported_usage(operation.protocol, bytes(content), speech=speech)
                runtime, lifecycle = await self._trusted_runtime_observation(operation, model, response)
                return RuntimeResult(
                    status_code=response.status_code,
                    body=bytes(content),
                    content_type=content_type,
                    elapsed_seconds=time.monotonic() - started,
                    runtime=runtime,
                    semantic_outcome=semantic,
                    usage=usage,
                    lifecycle=lifecycle,
                )
        except asyncio.CancelledError:
            raise
        except RuntimeOperationError:
            raise
        except FederationTransportError:
            raise RuntimeTransportError("federated transport failed") from None
        except httpx.TimeoutException:
            raise RuntimeTransportError("runtime request timed out") from None
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError):
            raise RuntimeTransportError("runtime transport failed") from None
        except (ValidationError, json.JSONDecodeError, UnicodeError, ValueError, TypeError, RecursionError):
            raise RuntimeProtocolError("runtime response is invalid") from None


class StubRuntimeClient(RuntimeClient):
    def __init__(self, results: dict[str, dict[str, Any]] | None = None) -> None:
        self.results = results or {}

    async def close(self) -> None:
        return None

    async def federation_health(self) -> dict[str, object]:
        return {"ready": True, "routes": 0, "circuits": {}}

    async def activate(self, model: OperationalModel, operation: ClaimedOperation) -> None:
        del model, operation

    async def observe(
        self, model: OperationalModel, operation: ClaimedOperation,
    ) -> tuple[RuntimeIdentity, RuntimeLifecycleObservation | None]:
        return RuntimeIdentity(gpu_count=model.gateway.gpu_allocation_count), None

    async def invoke(self, model: OperationalModel, operation: ClaimedOperation, request_body: bytes) -> RuntimeResult:
        del request_body
        payload = self.results.get(
            model.id, {"id": f"result-{operation.id}", "choices": [{"message": {"content": "ok"}}]}
        )
        body = json.dumps(payload).encode()
        return RuntimeResult(
            status_code=200,
            body=body,
            content_type="application/json",
            elapsed_seconds=0.01,
            runtime=RuntimeIdentity(
                pod_uid=f"pod-{model.id}",
                node_uid="node-test",
                gpu_uuids=["GPU-test"],
                gpu_count=model.gateway.gpu_allocation_count,
                preemptible=True,
            ),
            semantic_outcome=RuntimeClient._semantic_outcome(operation.protocol, body),
        )
