"""Credential-minimal, deadline-aware activation and inference HTTP adapter."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import httpx
from pydantic import ValidationError

from .federation import FederationRouter, FederationTransportError
from .models import ClaimedOperation, ReportedUsage, RuntimeIdentity, RuntimeLifecycleObservation, RuntimeResult
from .registry import OperationalModel, ProbeSpec


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


def sanitize_error_detail(value: str, limit: int = 200) -> str:
    """Return a bounded payload-independent detail for durable/public surfaces.

    Upstream exception strings are never suitable ledger data: an SDK may have
    embedded a prompt, response, URL, or credential in one. Error codes retain
    the actionable classification; the detail intentionally stays generic.
    """

    del limit
    if not value:
        return ""
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
    ) -> None:
        self.activation_timeout_seconds = activation_timeout_seconds
        self.runtime_timeout_seconds = runtime_timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self._owns_client = client is None
        self.metadata_provider = metadata_provider or NullRuntimeMetadataProvider()
        self.federation = federation or FederationRouter({})

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
    ) -> tuple[RuntimeIdentity, RuntimeLifecycleObservation | None]:
        try:
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
    def _reported_usage(protocol: str, body: bytes) -> ReportedUsage | None:
        """Extract optional OpenAI token totals without making usage part of protocol validity."""

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

    async def invoke(self, model: OperationalModel, operation: ClaimedOperation, request_body: bytes) -> RuntimeResult:
        try:
            endpoint = model.binding.endpoints[operation.protocol]
        except KeyError:
            raise RuntimeProtocolError("runtime protocol is invalid") from None
        headers = self._correlation_headers(operation)
        headers["content-type"] = operation.request_content_type
        started = time.monotonic()
        try:
            if model.binding.backend_class == "local-kubernetes":
                stream = self.client.stream(
                    "POST",
                    f"{model.binding.service_origin}{endpoint}",
                    headers=headers,
                    content=request_body,
                    timeout=self._timeout(operation, self.runtime_timeout_seconds),
                )
            else:
                stream = self.federation.stream(
                    model,
                    operation_id=operation.id,
                    method="POST",
                    path=endpoint,
                    timeout_seconds=self._timeout(operation, self.runtime_timeout_seconds),
                    content_type=operation.request_content_type,
                    content=request_body,
                )
            async with stream as response:
                content_type = self._content_type(response, operation.protocol)
                preempted = self._header(response, "x-fs2-preempted", maximum=16)
                if preempted is not None and preempted.lower() not in {"true", "false"}:
                    raise RuntimeProtocolError("runtime response header is invalid")
                if response.status_code in (409, 410) and preempted is not None and preempted.lower() == "true":
                    raise PreemptedError("runtime reported preemption")
                if not response.is_success:
                    # Failure bodies are deliberately never buffered or persisted.
                    runtime, lifecycle = await self._trusted_runtime_observation(operation, model)
                    return RuntimeResult(
                        status_code=response.status_code,
                        body=b"",
                        content_type=content_type,
                        elapsed_seconds=time.monotonic() - started,
                        runtime=runtime,
                        semantic_outcome="not_evaluated",
                        failure_code="upstream_http_error",
                        lifecycle=lifecycle,
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self.max_response_bytes:
                        raise RuntimeProtocolError("runtime response exceeded configured maximum")
                semantic = self._semantic_outcome(operation.protocol, bytes(content))
                runtime, lifecycle = await self._trusted_runtime_observation(operation, model)
                return RuntimeResult(
                    status_code=response.status_code,
                    body=bytes(content),
                    content_type=content_type,
                    elapsed_seconds=time.monotonic() - started,
                    runtime=runtime,
                    semantic_outcome=semantic,
                    usage=self._reported_usage(operation.protocol, bytes(content)),
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
