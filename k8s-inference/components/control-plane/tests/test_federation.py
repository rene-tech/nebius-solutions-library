from __future__ import annotations

import hashlib
import json
import ssl
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import certifi
import httpx
import jsonschema
import pytest
from conftest import CATALOG_ROOT, CONTROL_ROOT, REPO_ROOT
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fs2_serve_catalog.loader import load_catalog

from fs2_serve.federation import (
    FEDERATION_ROUTES_SCHEMA,
    FederationConfigError,
    FederationRouter,
)
from fs2_serve.models import ClaimedOperation, OperationStatus
from fs2_serve.registry import OperationalModel
from fs2_serve.runtime import RuntimeClient, RuntimeTransportError
from fs2_serve.settings import Settings


def _operation(model: OperationalModel) -> ClaimedOperation:
    now = datetime.now(UTC)
    return ClaimedOperation(
        id=uuid4(),
        tenant_id="tenant-private",
        principal_id="principal-private",
        token_id=uuid4(),
        model_id=model.id,
        model_revision=model.model_revision,
        protocol="openai-chat",
        operation="chat",
        idempotency_key="federation-operation-key",
        status=OperationStatus.ACTIVATING,
        accepted_at=now,
        available_at=now,
        deadline_at=now + timedelta(seconds=10),
        payload_expires_at=now + timedelta(seconds=60),
        attempt=1,
        max_attempts=2,
        fencing_token=1,
        request_content_type="application/json",
        worker_id="worker-federation",
    )


def _federated_model(registry, trust_digest: str) -> OperationalModel:
    local = registry.get("qwen3-8b")
    endpoint_identity = hashlib.sha256(b"unit-exact-upstream-identity").hexdigest()
    binding = replace(
        local.binding,
        backend_class="federated-kserve-nim",
        backend_region="us-central1",
        backend_endpoint_identity_sha256=endpoint_identity,
        backend_trust_bundle_sha256=trust_digest,
        backend_credential_requirement_id="fs2-models/qwen-upstream",
    )
    gateway = replace(local.gateway, binding=binding)
    return replace(local, gateway=gateway)


def _route_document(model: OperationalModel, *, credential_mode: str = "bearer") -> dict[str, Any]:
    binding = model.binding
    return {
        "schema": FEDERATION_ROUTES_SCHEMA,
        "routes": {
            model.id: {
                "backend": {
                    "model_digest": binding.model_digest,
                    "backend_class": binding.backend_class,
                    "runtime_image_digest": binding.backend_runtime_image_digest,
                    "endpoint_identity_sha256": binding.backend_endpoint_identity_sha256,
                    "trust_bundle_sha256": binding.backend_trust_bundle_sha256,
                    "credential_requirement_id": binding.backend_credential_requirement_id,
                },
                "destination": {
                    "scheme": "https",
                    "host": "upstream.unit.invalid",
                    "port": 443,
                    "connect_ips": ["8.8.8.8"],
                },
                "credential_mode": credential_mode,
                "health": {
                    "method": "GET",
                    "path": "/federation-health",
                    "expected_status": 200,
                    "timeout_seconds": 2,
                },
                "timeouts": {
                    "connect_seconds": 1,
                    "read_seconds": 3,
                    "write_seconds": 2,
                    "pool_seconds": 1,
                    "total_seconds": 5,
                },
                "idempotency": {"mode": "operation-id", "header": "Idempotency-Key"},
                "retry": {
                    "max_attempts": 2,
                    "base_backoff_seconds": 0.01,
                    "retry_status_codes": [429, 502, 503, 504],
                },
                "circuit_breaker": {"failure_threshold": 2, "recovery_seconds": 30},
            }
        },
    }


def _secret_fixture(tmp_path: Path) -> tuple[Path, str]:
    secret_root = tmp_path / "federation"
    secret_root.mkdir()
    ca_bytes = Path(certifi.where()).read_bytes()
    (secret_root / "qwen-upstream.ca.pem").write_bytes(ca_bytes)
    (secret_root / "qwen-upstream.bearer").write_text("federation-test-value-one\n")
    return secret_root, hashlib.sha256(ca_bytes).hexdigest()


def _write_routes(secret_root: Path, document: dict[str, Any]) -> Path:
    path = secret_root / "routes.json"
    path.write_text(json.dumps(document) + "\n")
    return path


def _router(
    tmp_path: Path,
    registry,
    handler,
    *,
    document_update=None,
) -> tuple[FederationRouter, OperationalModel, list[httpx.AsyncClient]]:
    secret_root, trust_digest = _secret_fixture(tmp_path)
    model = _federated_model(registry, trust_digest)
    document = _route_document(model)
    if document_update is not None:
        document_update(document)
    path = _write_routes(secret_root, document)
    clients: list[httpx.AsyncClient] = []

    def factory(_route) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False)
        clients.append(client)
        return client

    router = FederationRouter.load(path, [model], secret_root=secret_root, client_factory=factory)
    return router, model, clients


def test_federation_schema_and_settings_are_exact_and_secret_mounted(tmp_path: Path, registry) -> None:
    secret_root, trust_digest = _secret_fixture(tmp_path)
    model = _federated_model(registry, trust_digest)
    document = _route_document(model)
    schema = json.loads((CONTROL_ROOT / "contracts" / "federation-routes.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(document)
    route_path = _write_routes(secret_root, document)
    settings = Settings(federation_routes_file=route_path, federation_secret_dir=secret_root)
    assert settings.federation_routes_file.parent == settings.federation_secret_dir
    with pytest.raises(ValueError, match="directly inside"):
        Settings(federation_routes_file=tmp_path / "elsewhere.json", federation_secret_dir=secret_root)


def test_federation_missing_or_extra_routes_fail_closed(tmp_path: Path, registry) -> None:
    secret_root, trust_digest = _secret_fixture(tmp_path)
    model = _federated_model(registry, trust_digest)
    with pytest.raises(FederationConfigError, match="lacks transport"):
        FederationRouter.load(secret_root / "missing.json", [model], secret_root=secret_root)
    _write_routes(secret_root, {"schema": FEDERATION_ROUTES_SCHEMA, "routes": {}})
    with pytest.raises(FederationConfigError, match="enabled signed bindings"):
        FederationRouter.load(secret_root / "routes.json", [model], secret_root=secret_root)
    assert (
        FederationRouter.load(secret_root / "missing-again.json", registry.list(), secret_root=secret_root).routes == {}
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("model_digest", "1" * 64),
        ("runtime_image_digest", "sha256:" + "2" * 64),
        ("endpoint_identity_sha256", "3" * 64),
        ("trust_bundle_sha256", "4" * 64),
        ("credential_requirement_id", "fs2-models/different-upstream"),
    ],
)
def test_federation_never_aliases_changed_model_runtime_endpoint_or_trust(
    tmp_path: Path, registry, field: str, replacement: str
) -> None:
    secret_root, trust_digest = _secret_fixture(tmp_path)
    model = _federated_model(registry, trust_digest)
    document = _route_document(model)
    document["routes"][model.id]["backend"][field] = replacement
    path = _write_routes(secret_root, document)
    with pytest.raises(FederationConfigError) as captured:
        FederationRouter.load(path, [model], secret_root=secret_root)
    assert replacement not in str(captured.value)
    assert captured.value.__cause__ is None


def test_federation_rejects_tampered_mounted_trust_bundle(tmp_path: Path, registry) -> None:
    secret_root, trust_digest = _secret_fixture(tmp_path)
    model = _federated_model(registry, trust_digest)
    (secret_root / "qwen-upstream.ca.pem").write_bytes((secret_root / "qwen-upstream.ca.pem").read_bytes() + b"\n")
    path = _write_routes(secret_root, _route_document(model))
    with pytest.raises(FederationConfigError, match="trust bundle") as captured:
        FederationRouter.load(path, [model], secret_root=secret_root)
    assert trust_digest not in str(captured.value)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("scheme", "http"),
        ("host", "*.unit.invalid"),
        ("host", "UPSTREAM.unit.invalid"),
        ("connect_ips", ["127.0.0.1"]),
        ("connect_ips", ["169.254.169.254"]),
        ("connect_ips", ["10.0.0.1"]),
    ],
)
def test_federation_rejects_unsafe_destinations(tmp_path: Path, registry, field: str, replacement: Any) -> None:
    secret_root, trust_digest = _secret_fixture(tmp_path)
    model = _federated_model(registry, trust_digest)
    document = _route_document(model)
    document["routes"][model.id]["destination"][field] = replacement
    path = _write_routes(secret_root, document)
    with pytest.raises(FederationConfigError) as captured:
        FederationRouter.load(path, [model], secret_root=secret_root)
    assert "UPSTREAM" not in str(captured.value) and "169.254" not in str(captured.value)


@pytest.mark.asyncio
async def test_federated_activation_and_invoke_use_separate_probes_stable_idempotency_and_no_pat_identity(
    tmp_path: Path, registry
) -> None:
    requests: list[httpx.Request] = []
    invoke_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal invoke_count
        requests.append(request)
        if request.url.path in {"/federation-health", "/health"}:
            return httpx.Response(200)
        if request.url.path == "/v1/chat/completions":
            invoke_count += 1
            if invoke_count == 1:
                return httpx.Response(503, headers={"content-type": "application/json"})
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b'{"choices":[{"message":{"content":"ok"}}]}',
            )
        raise AssertionError("unexpected federated path")

    router, model, clients = _router(tmp_path, registry, handler)

    async def local_handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("federated work reached the local runtime transport")

    local = httpx.AsyncClient(transport=httpx.MockTransport(local_handler), trust_env=False)
    runtime = RuntimeClient(
        activation_timeout_seconds=3,
        runtime_timeout_seconds=3,
        max_response_bytes=1024,
        client=local,
        federation=router,
    )
    operation = _operation(model)
    body = b'{"model":"qwen3-8b","messages":[{"role":"user","content":"private"}]}'
    try:
        await runtime.activate(model, operation)
        result = await runtime.invoke(model, operation, body)
        assert result.status_code == 200 and result.semantic_outcome == "protocol_valid"
        assert [request.url.path for request in requests] == [
            "/federation-health",
            "/health",
            "/v1/chat/completions",
            "/v1/chat/completions",
        ]
        for request in requests:
            assert request.url.host == "8.8.8.8"
            assert request.headers["host"] == "upstream.unit.invalid"
            assert request.headers["x-fs2-operation-id"] == str(operation.id)
            assert request.headers["idempotency-key"] == str(operation.id)
            assert request.extensions["sni_hostname"] == "upstream.unit.invalid"
            rendered = repr(dict(request.headers))
            assert operation.tenant_id not in rendered
            assert operation.principal_id not in rendered
            assert str(operation.token_id) not in rendered
        assert requests[-1].headers["authorization"] == "Bearer federation-test-value-one"
        assert all(client._trust_env is False for client in clients)  # type: ignore[attr-defined]
        assert await runtime.federation_health() == {
            "ready": True,
            "routes": 1,
            "circuits": {"qwen3-8b": "closed"},
        }
    finally:
        await runtime.close()
        await local.aclose()


@pytest.mark.asyncio
async def test_federation_circuit_opens_after_bounded_failure_without_leaking_destination(
    tmp_path: Path, registry
) -> None:
    calls = 0
    private_host = "upstream.unit.invalid"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(f"do not leak {private_host}", request=request)

    def configure(document: dict[str, Any]) -> None:
        route = document["routes"]["qwen3-8b"]
        route["retry"]["max_attempts"] = 1
        route["circuit_breaker"]["failure_threshold"] = 1

    router, model, _ = _router(tmp_path, registry, handler, document_update=configure)
    runtime = RuntimeClient(
        activation_timeout_seconds=2,
        runtime_timeout_seconds=2,
        max_response_bytes=1024,
        federation=router,
    )
    operation = _operation(model)
    try:
        for _ in range(2):
            with pytest.raises(RuntimeTransportError) as captured:
                await runtime.invoke(model, operation, b'{"model":"qwen3-8b"}')
            rendered = "".join(traceback.format_exception(captured.value))
            assert private_host not in rendered
            assert captured.value.__cause__ is None
        assert calls == 1
        assert await runtime.federation_health() == {
            "ready": False,
            "routes": 1,
            "circuits": {"qwen3-8b": "open"},
        }
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_bearer_rotation_is_stable_within_retry_and_visible_to_the_next_operation(
    tmp_path: Path, registry
) -> None:
    authorizations: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers["authorization"])
        if len(authorizations) == 1:
            (tmp_path / "federation" / "qwen-upstream.bearer").write_text("federation-test-value-two\n")
            return httpx.Response(503, headers={"content-type": "application/json"})
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"choices":[{"message":{"content":"ok"}}]}',
        )

    router, model, _ = _router(tmp_path, registry, handler)
    runtime = RuntimeClient(
        activation_timeout_seconds=2,
        runtime_timeout_seconds=2,
        max_response_bytes=1024,
        federation=router,
    )
    try:
        await runtime.invoke(model, _operation(model), b'{"model":"qwen3-8b"}')
        await runtime.invoke(model, _operation(model), b'{"model":"qwen3-8b"}')
        assert authorizations == [
            "Bearer federation-test-value-one",
            "Bearer federation-test-value-one",
            "Bearer federation-test-value-two",
        ]
    finally:
        await runtime.close()


def _write_mtls_identity(secret_root: Path) -> str:
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fs2 unit CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fs2 gateway")])
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(client_name)
        .issuer_name(ca_name)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(ca_key, hashes.SHA256())
    )
    ca_bytes = ca_cert.public_bytes(serialization.Encoding.PEM)
    (secret_root / "qwen-upstream.ca.pem").write_bytes(ca_bytes)
    (secret_root / "qwen-upstream.client.crt").write_bytes(client_cert.public_bytes(serialization.Encoding.PEM))
    (secret_root / "qwen-upstream.client.key").write_bytes(
        client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return hashlib.sha256(ca_bytes).hexdigest()


@pytest.mark.asyncio
async def test_mtls_route_loads_exact_trust_and_client_identity_without_bearer(tmp_path: Path, registry) -> None:
    secret_root = tmp_path / "federation"
    secret_root.mkdir()
    trust_digest = _write_mtls_identity(secret_root)
    model = _federated_model(registry, trust_digest)
    document = _route_document(model, credential_mode="mtls")
    path = _write_routes(secret_root, document)

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    def factory(_route) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)

    router = FederationRouter.load(path, [model], secret_root=secret_root, client_factory=factory)
    route = router.routes[model.id]
    try:
        assert route.ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert route.ssl_context.check_hostname is True
        assert route.ssl_context.minimum_version >= ssl.TLSVersion.TLSv1_2
        assert not (secret_root / "qwen-upstream.bearer").exists()
        assert await router.probe_health(model, uuid4(), 2) is True
        assert "authorization" not in requests[0].headers
    finally:
        await router.close()


def test_current_exact_sm90_candidates_remain_route_disabled_and_public_data_has_no_origin_or_secret(registry) -> None:
    catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
    expected = {
        "molmim": (
            "gated",
            "best-current-exact-upstream",
            "sha256:7700c5556935a93055bee5367d36acb6d3e55d22fd1ba28503f5447656fa63fa",
        ),
        "evo2-40b": (
            "credential-compromised",
            "candidate-after-remediation",
            "sha256:561886bab1d2d0da836ebf5bec403f9de2baf6e92deb7eedf1b316aa994b5dd2",
        ),
        "diffdock": (
            "disabled",
            "none",
            "sha256:300696eb8331d78face40f84d835cc1e278c7d3c391c5aabbbee5884366da480",
        ),
        "rfdiffusion": (
            "disabled",
            "none",
            "sha256:15e40e466d8ebe9a53f1feea599373720428c9de65da750bf4271c96ec35ceb4",
        ),
        "proteinmpnn": (
            "disabled",
            "none",
            "sha256:b55a0aa6733e267e6e6fe06434e98aea61eff14bc5545127555607fef6f38aa5",
        ),
    }
    for model_id, (route_state, preference, runtime_image_digest) in expected.items():
        backend = catalog.federated_backend(model_id)
        assert backend is not None
        assert backend.route_state == route_state
        backend_value = backend.to_dict()
        assert backend_value["preference"] == preference
        assert backend_value["runtime_image_digest"] == runtime_image_digest
    public = json.dumps(registry.render_runtime_config())
    for forbidden in (
        "service_origin",
        "activation_url",
        "upstream.unit.invalid",
        "federation-test-value-one",
    ):
        assert forbidden not in public


def test_duplicate_or_secret_bearing_invalid_configuration_never_reflects_input(tmp_path: Path, registry) -> None:
    secret_root, trust_digest = _secret_fixture(tmp_path)
    model = _federated_model(registry, trust_digest)
    private = "private-upstream-name.unit.invalid"
    path = secret_root / "routes.json"
    path.write_text(
        '{"schema":"fs2-serve.nebius.ai/federation-routes/v1",'
        f'"routes":{{"{model.id}":{{"destination":{{"host":"{private}"}}}},'
        f'"{model.id}":{{"destination":{{"host":"{private}"}}}}}}}}'
    )
    with pytest.raises(FederationConfigError) as captured:
        FederationRouter.load(path, [model], secret_root=secret_root)
    assert private not in str(captured.value)
    assert captured.value.__cause__ is None
