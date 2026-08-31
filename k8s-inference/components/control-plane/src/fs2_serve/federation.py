"""Fail-closed transport for exact, signed federated serving bindings."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import ssl
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .registry import OperationalModel

FEDERATION_ROUTES_SCHEMA = "fs2-serve.nebius.ai/federation-routes/v1"
_MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_SECRET_BYTES = 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MODEL_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,126}[a-z0-9])?$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_REQUIREMENT_ID = re.compile(r"^fs2-models/([a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)$")
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class FederationConfigError(ValueError):
    """The secret-mounted route contract does not match the signed catalog."""


class FederationTransportError(RuntimeError):
    """A bounded, payload-free federated transport failure."""


class _Destination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheme: Literal["https"]
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    connect_ips: list[str] = Field(min_length=1, max_length=8)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        normalized = value.lower().rstrip(".")
        if normalized != value or any(_DNS_LABEL.fullmatch(label) is None for label in value.split(".")):
            raise ValueError("destination host is invalid")
        return value

    @field_validator("connect_ips")
    @classmethod
    def validate_ips(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            address = ipaddress.ip_address(item)
            # Federated routes are external. Private, loopback, link-local,
            # multicast, unspecified, and documentation ranges fail closed.
            if not address.is_global:
                raise ValueError("federated destination is not globally routable")
            rendered = address.compressed
            if rendered in normalized:
                raise ValueError("duplicate destination address")
            normalized.append(rendered)
        return normalized


class _Backend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_digest: str
    backend_class: Literal["federated-kserve-nim", "federated-serverless"]
    runtime_image_digest: str
    endpoint_identity_sha256: str
    trust_bundle_sha256: str
    credential_requirement_id: str

    @field_validator("model_digest", "endpoint_identity_sha256", "trust_bundle_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("digest is invalid")
        return value

    @field_validator("runtime_image_digest")
    @classmethod
    def validate_image_digest(cls, value: str) -> str:
        if _IMAGE_DIGEST.fullmatch(value) is None:
            raise ValueError("image digest is invalid")
        return value

    @field_validator("credential_requirement_id")
    @classmethod
    def validate_requirement(cls, value: str) -> str:
        if _REQUIREMENT_ID.fullmatch(value) is None:
            raise ValueError("credential requirement is invalid")
        return value


class _Health(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET"]
    path: str = Field(min_length=1, max_length=256)
    expected_status: int = Field(ge=100, le=599)
    timeout_seconds: float = Field(gt=0, le=5, allow_inf_nan=False)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or "?" in value or "#" in value:
            raise ValueError("health path is invalid")
        return value


class _Timeouts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connect_seconds: float = Field(gt=0, le=3, allow_inf_nan=False)
    read_seconds: float = Field(gt=0, le=30, allow_inf_nan=False)
    write_seconds: float = Field(gt=0, le=10, allow_inf_nan=False)
    pool_seconds: float = Field(gt=0, le=3, allow_inf_nan=False)
    total_seconds: float = Field(gt=0, le=30, allow_inf_nan=False)


class _Retry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(ge=1, le=3)
    base_backoff_seconds: float = Field(ge=0.01, le=1, allow_inf_nan=False)
    retry_status_codes: list[int] = Field(max_length=7)

    @field_validator("retry_status_codes")
    @classmethod
    def validate_statuses(cls, value: list[int]) -> list[int]:
        if value != sorted(set(value)) or any(status not in _RETRYABLE_STATUS for status in value):
            raise ValueError("retry status set is invalid")
        return value


class _CircuitBreaker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = Field(ge=1, le=10)
    recovery_seconds: float = Field(ge=1, le=300, allow_inf_nan=False)


class _Idempotency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["operation-id"]
    header: Literal["Idempotency-Key"]


class _RouteValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: _Backend
    destination: _Destination
    credential_mode: Literal["bearer", "mtls"]
    health: _Health
    timeouts: _Timeouts
    idempotency: _Idempotency
    retry: _Retry
    circuit_breaker: _CircuitBreaker


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_name: str = Field(alias="schema")
    routes: dict[str, _RouteValue] = Field(max_length=64)

    @model_validator(mode="after")
    def validate_schema_and_keys(self) -> _Document:
        if self.schema_name != FEDERATION_ROUTES_SCHEMA:
            raise ValueError("federation route schema is unsupported")
        if any(_MODEL_ID.fullmatch(model_id) is None for model_id in self.routes):
            raise ValueError("federation route model ID is invalid")
        return self


@dataclass(frozen=True)
class FederatedRoute:
    model_id: str
    backend: _Backend
    destination: _Destination
    credential_mode: Literal["bearer", "mtls"]
    health: _Health
    timeouts: _Timeouts
    idempotency: _Idempotency
    retry: _Retry
    circuit_breaker: _CircuitBreaker
    secret_root: Path
    ssl_context: ssl.SSLContext = field(repr=False, compare=False)

    @property
    def credential_slug(self) -> str:
        match = _REQUIREMENT_ID.fullmatch(self.backend.credential_requirement_id)
        assert match is not None
        return match.group(1)

    @property
    def host_header(self) -> str:
        return (
            self.destination.host
            if self.destination.port == 443
            else f"{self.destination.host}:{self.destination.port}"
        )

    def url(self, path: str, attempt: int) -> str:
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise FederationTransportError("federated route path is invalid")
        address = self.destination.connect_ips[attempt % len(self.destination.connect_ips)]
        authority = f"[{address}]" if ":" in address else address
        if self.destination.port != 443:
            authority = f"{authority}:{self.destination.port}"
        return f"https://{authority}{path}"

    def bearer_token(self) -> str:
        if self.credential_mode != "bearer":
            raise FederationTransportError("federated credential mode is invalid")
        try:
            raw = _bounded_file(self.secret_root / f"{self.credential_slug}.bearer")
        except FederationConfigError:
            raise FederationTransportError("federated credential is unavailable") from None
        try:
            token = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            raise FederationTransportError("federated credential is unavailable") from None
        if not 16 <= len(token) <= 8192 or any(character.isspace() or ord(character) < 33 for character in token):
            raise FederationTransportError("federated credential is unavailable")
        return token


@dataclass
class _CircuitState:
    threshold: int
    recovery_seconds: float
    failures: int = 0
    open_until: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def assert_available(self) -> None:
        async with self.lock:
            if time.monotonic() < self.open_until:
                raise FederationTransportError("federated upstream is unavailable")

    async def succeeded(self) -> None:
        async with self.lock:
            self.failures = 0
            self.open_until = 0

    async def failed(self) -> None:
        async with self.lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.open_until = time.monotonic() + self.recovery_seconds

    async def snapshot(self) -> str:
        async with self.lock:
            return "open" if time.monotonic() < self.open_until else "closed"


ClientFactory = Callable[[FederatedRoute], httpx.AsyncClient]


def _bounded_file(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if not 1 <= size <= _MAX_SECRET_BYTES:
            raise FederationConfigError("federation secret file is invalid")
        return path.read_bytes()
    except FederationConfigError:
        raise
    except (OSError, ValueError):
        raise FederationConfigError("federation secret file is unavailable") from None


def _ssl_context(route: _RouteValue, secret_root: Path) -> ssl.SSLContext:
    match = _REQUIREMENT_ID.fullmatch(route.backend.credential_requirement_id)
    assert match is not None
    slug = match.group(1)
    ca_path = secret_root / f"{slug}.ca.pem"
    ca_bytes = _bounded_file(ca_path)
    if not hashlib.sha256(ca_bytes).hexdigest() == route.backend.trust_bundle_sha256:
        raise FederationConfigError("federation trust bundle differs from the signed binding")
    try:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_path))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        if route.credential_mode == "mtls":
            cert_path = secret_root / f"{slug}.client.crt"
            key_path = secret_root / f"{slug}.client.key"
            _bounded_file(cert_path)
            _bounded_file(key_path)
            context.load_cert_chain(str(cert_path), str(key_path))
        else:
            # Validate availability without retaining the plaintext value.
            token = _bounded_file(secret_root / f"{slug}.bearer")
            try:
                decoded = token.decode("ascii").strip()
            except UnicodeDecodeError:
                raise FederationConfigError("federation credential is invalid") from None
            if not 16 <= len(decoded) <= 8192 or any(
                character.isspace() or ord(character) < 33 for character in decoded
            ):
                raise FederationConfigError("federation credential is invalid")
        return context
    except FederationConfigError:
        raise
    except (OSError, ssl.SSLError, ValueError):
        raise FederationConfigError("federation TLS material is invalid") from None


def _default_client(route: FederatedRoute) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        verify=route.ssl_context,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4, keepalive_expiry=15),
    )


class FederationRouter:
    """Secret-backed transport that cannot independently make a model routable."""

    def __init__(
        self,
        routes: Mapping[str, FederatedRoute],
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.client_factory = client_factory or _default_client
        self.routes = dict(routes)
        self.clients: dict[str, httpx.AsyncClient] = {}
        self.circuits = {
            model_id: _CircuitState(route.circuit_breaker.failure_threshold, route.circuit_breaker.recovery_seconds)
            for model_id, route in self.routes.items()
        }

    @classmethod
    def load(
        cls,
        path: Path,
        models: Iterable[OperationalModel],
        *,
        secret_root: Path,
        client_factory: ClientFactory | None = None,
    ) -> FederationRouter:
        model_map = {model.id: model for model in models}
        expected = {
            model.id
            for model in model_map.values()
            if model.enabled and model.binding.backend_class != "local-kubernetes"
        }
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            if expected:
                raise FederationConfigError("a routable federated binding lacks transport configuration") from None
            return cls({}, client_factory=client_factory)
        except OSError:
            raise FederationConfigError("federation route configuration is unavailable") from None
        if not 1 <= len(raw) <= _MAX_DOCUMENT_BYTES:
            raise FederationConfigError("federation route configuration is invalid")

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            value: dict[str, object] = {}
            for key, item in pairs:
                if key in value:
                    raise FederationConfigError("federation route configuration contains duplicate keys")
                value[key] = item
            return value

        try:
            parsed = json.loads(raw, object_pairs_hook=reject_duplicates)
            document = _Document.model_validate(parsed)
        except FederationConfigError:
            raise
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError, TypeError, RecursionError):
            # Pydantic errors contain rejected input values, including a private
            # upstream host. Suppress the context and keep the error generic.
            raise FederationConfigError("federation route configuration is invalid") from None
        if set(document.routes) != expected:
            raise FederationConfigError("federation routes differ from enabled signed bindings")

        routes: dict[str, FederatedRoute] = {}
        for model_id, value in document.routes.items():
            model = model_map[model_id]
            binding = model.binding
            exact = (
                value.backend.model_digest == binding.model_digest
                and value.backend.backend_class == binding.backend_class
                and value.backend.runtime_image_digest == binding.backend_runtime_image_digest
                and value.backend.endpoint_identity_sha256 == binding.backend_endpoint_identity_sha256
                and value.backend.trust_bundle_sha256 == binding.backend_trust_bundle_sha256
                and value.backend.credential_requirement_id == binding.backend_credential_requirement_id
            )
            if not exact:
                raise FederationConfigError("federation route differs from the exact signed binding")
            context = _ssl_context(value, secret_root)
            routes[model_id] = FederatedRoute(
                model_id=model_id,
                backend=value.backend,
                destination=value.destination,
                credential_mode=value.credential_mode,
                health=value.health,
                timeouts=value.timeouts,
                idempotency=value.idempotency,
                retry=value.retry,
                circuit_breaker=value.circuit_breaker,
                secret_root=secret_root,
                ssl_context=context,
            )
        return cls(routes, client_factory=client_factory)

    def has_route(self, model: OperationalModel) -> bool:
        return model.id in self.routes

    async def close(self) -> None:
        await asyncio.gather(*(client.aclose() for client in self.clients.values()))

    def _client(self, model_id: str, route: FederatedRoute) -> httpx.AsyncClient:
        client = self.clients.get(model_id)
        if client is None:
            client = self.client_factory(route)
            self.clients[model_id] = client
        return client

    async def health(self) -> dict[str, object]:
        """Return an origin- and credential-free circuit/readiness projection."""

        states = {model_id: await self.circuits[model_id].snapshot() for model_id in sorted(self.routes)}
        return {
            "ready": all(state == "closed" for state in states.values()),
            "routes": len(states),
            "circuits": states,
        }

    @staticmethod
    def _timeout(route: FederatedRoute, limit: float) -> httpx.Timeout:
        return httpx.Timeout(
            timeout=min(limit, route.timeouts.read_seconds),
            connect=min(limit, route.timeouts.connect_seconds),
            read=min(limit, route.timeouts.read_seconds),
            write=min(limit, route.timeouts.write_seconds),
            pool=min(limit, route.timeouts.pool_seconds),
        )

    @staticmethod
    def _headers(
        route: FederatedRoute,
        operation_id: UUID,
        *,
        content_type: str | None,
        bearer_token: str | None,
    ) -> dict[str, str]:
        headers = {
            "host": route.host_header,
            "x-fs2-operation-id": str(operation_id),
            route.idempotency.header: str(operation_id),
        }
        if content_type is not None:
            headers["content-type"] = content_type
        if bearer_token is not None:
            headers["authorization"] = f"Bearer {bearer_token}"
        return headers

    @asynccontextmanager
    async def stream(
        self,
        model: OperationalModel,
        *,
        operation_id: UUID,
        method: str,
        path: str,
        timeout_seconds: float,
        content_type: str | None = None,
        content: bytes | None = None,
    ) -> AsyncIterator[httpx.Response]:
        try:
            route = self.routes[model.id]
            client = self._client(model.id, route)
            circuit = self.circuits[model.id]
        except KeyError:
            raise FederationTransportError("federated route is unavailable") from None
        await circuit.assert_available()
        retry_statuses = set(route.retry.retry_status_codes)
        deadline = time.monotonic() + min(timeout_seconds, route.timeouts.total_seconds)
        # Freeze the scoped credential for this bounded logical request so a
        # rotation between attempts cannot change the upstream idempotency
        # principal. The next request re-reads the projected Secret.
        bearer_token = route.bearer_token() if route.credential_mode == "bearer" else None
        for attempt in range(route.retry.max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await circuit.failed()
                raise FederationTransportError("federated request deadline elapsed")
            context = client.stream(
                method,
                route.url(path, attempt),
                headers=self._headers(
                    route,
                    operation_id,
                    content_type=content_type,
                    bearer_token=bearer_token,
                ),
                content=content,
                timeout=self._timeout(route, remaining),
                follow_redirects=False,
                extensions={"sni_hostname": route.destination.host},
            )
            try:
                response = await context.__aenter__()
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError, UnicodeError, ValueError, TypeError):
                if attempt + 1 < route.retry.max_attempts:
                    delay = min(route.retry.base_backoff_seconds * (2**attempt), deadline - time.monotonic())
                    if delay <= 0:
                        await circuit.failed()
                        raise FederationTransportError("federated request deadline elapsed") from None
                    await asyncio.sleep(delay)
                    continue
                await circuit.failed()
                raise FederationTransportError("federated transport failed") from None
            if response.status_code in retry_statuses and attempt + 1 < route.retry.max_attempts:
                await context.__aexit__(None, None, None)
                delay = min(route.retry.base_backoff_seconds * (2**attempt), deadline - time.monotonic())
                if delay <= 0:
                    await circuit.failed()
                    raise FederationTransportError("federated request deadline elapsed")
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 500:
                await circuit.failed()
            else:
                await circuit.succeeded()
            try:
                yield response
            finally:
                await context.__aexit__(None, None, None)
            return
        raise FederationTransportError("federated transport failed")

    async def probe_health(self, model: OperationalModel, operation_id: UUID, timeout_seconds: float) -> bool:
        route = self.routes.get(model.id)
        if route is None:
            raise FederationTransportError("federated route is unavailable")
        async with self.stream(
            model,
            operation_id=operation_id,
            method=route.health.method,
            path=route.health.path,
            timeout_seconds=min(timeout_seconds, route.health.timeout_seconds),
        ) as response:
            return response.status_code == route.health.expected_status

    async def probe_readiness(
        self,
        model: OperationalModel,
        operation_id: UUID,
        *,
        method: str,
        path: str,
        expected_status: int,
        timeout_seconds: float,
    ) -> bool:
        async with self.stream(
            model,
            operation_id=operation_id,
            method=method,
            path=path,
            timeout_seconds=timeout_seconds,
        ) as response:
            return response.status_code == expected_status
