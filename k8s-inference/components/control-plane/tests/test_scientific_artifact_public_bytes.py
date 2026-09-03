"""The public artifact byte path: gateway-only upload, download and MCP parity.

Every test here drives the same surface an external customer reaches, so a
caller that can talk only to the public API can complete the whole flow:
reserve an upload, write the bytes, finalize, and read the bytes back. The
object store is deliberately unreachable from the client in these tests, which
is exactly the situation the inline path exists for.

Two properties are asserted repeatedly because they are the ones that matter:
an artifact is immutably bound to its declared digest, size and media type, and
an artifact is visible only to the tenant that created it.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.mcpserver import Context
from test_scientific_artifacts import ALLOWED_MEDIA_TYPES, FakeObjectStore, digest
from test_scientific_batch_production import (
    _MemoryInputUploadPort,
    profile_catalog,
    scientific_runtime,
)

from fs2_serve.api import AppRuntime, create_app
from fs2_serve.mcp_server import CORE_TOOLS, PATTokenVerifier, build_mcp_server
from fs2_serve.models import Scope, TokenCreate
from fs2_serve.scientific_artifacts import (
    MemoryArtifactRepository,
    ScientificArtifactService,
)
from fs2_serve.scientific_input_uploads import ScientificInputUploadService, content_path

PAYLOAD = b">target\nMKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQ\n"
MEDIA_TYPE = "text/x-fasta"
BASE_URL = "https://inference.test.invalid"


def _upload_request(payload: bytes = PAYLOAD, **overrides: Any) -> dict[str, Any]:
    request = {
        "model_id": "protein-design",
        "sha256": digest(payload).removeprefix("sha256:"),
        "size_bytes": len(payload),
        "media_type": MEDIA_TYPE,
    }
    request.update(overrides)
    return request


def _artifact_plane(runtime: AppRuntime, **service_kwargs: Any) -> tuple[FakeObjectStore, MemoryArtifactRepository]:
    """Attach a real artifact service over an in-memory store and repository."""

    object_store = FakeObjectStore(clock=lambda: datetime.now(UTC))
    repository = MemoryArtifactRepository()
    service = ScientificArtifactService(
        repository=repository,
        object_store=object_store,
        allowed_media_types=ALLOWED_MEDIA_TYPES,
        **service_kwargs,
    )
    runtime.artifact_service = service
    runtime.scientific_input_uploads = ScientificInputUploadService(
        store=runtime.store,
        artifacts=_MemoryInputUploadPort(repository, service),
        profiles=profile_catalog(),
    )
    return object_store, repository


async def _token(runtime: AppRuntime, *, principal_id: str, tenant_id: str) -> str:
    issued = await runtime.tokens.issue(
        TokenCreate(
            principal_id=principal_id,
            tenant_id=tenant_id,
            scopes={
                Scope.INFERENCE_INVOKE,
                Scope.OPERATIONS_READ,
                Scope.OPERATIONS_RESULT,
                Scope.MCP_INVOKE,
            },
            models={"protein-design"},
            max_concurrency=4,
        ),
        created_by="test",
    )
    return str(issued.token)


def _client(app: Any, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        headers={"authorization": f"Bearer {token}"},
    )


async def _begin(client: httpx.AsyncClient, *, key: str, request: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        "/v1/scientific-artifacts/uploads",
        headers={"idempotency-key": key},
        json=request,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _put(
    client: httpx.AsyncClient,
    reservation: dict[str, Any],
    payload: bytes,
    *,
    media_type: str = MEDIA_TYPE,
) -> httpx.Response:
    return await client.put(
        reservation["content_path"],
        content=payload,
        headers={"content-type": media_type},
    )


@pytest.mark.asyncio
async def test_gateway_only_client_uploads_finalizes_and_reads_exact_bytes(registry, cipher, hasher) -> None:
    """An external client completes the whole flow without touching the store."""

    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    object_store, _ = _artifact_plane(runtime)
    token = await _token(runtime, principal_id="scientist-a", tenant_id="tenant-a")
    app = create_app(runtime)
    async with app.router.lifespan_context(app), _client(app, token) as client:
        reservation = await _begin(client, key="public-bytes-0001", request=_upload_request())
        assert reservation["content_path"].startswith("/v1/scientific-artifacts/uploads/")
        assert reservation["max_content_bytes"] > len(PAYLOAD)
        # The presigned handle is advertised too, but is never used here.
        assert reservation["handle"]["method"] == "PUT"

        written = await _put(client, reservation, PAYLOAD)
        assert written.status_code == 200, written.text
        receipt = written.json()
        assert receipt["sha256"] == digest(PAYLOAD).removeprefix("sha256:")
        assert receipt["size_bytes"] == len(PAYLOAD)
        assert receipt["media_type"] == MEDIA_TYPE
        assert receipt["finalized"] is False
        assert written.headers["x-fs2-artifact-sha256"] == receipt["sha256"]

        finalized = await client.post(
            f"/v1/scientific-artifacts/uploads/{reservation['upload_id']}:finalize",
            json={"operation_id": reservation["operation_id"]},
        )
        assert finalized.status_code == 200, finalized.text
        pointer = finalized.json()
        assert pointer["sha256"] == digest(PAYLOAD).removeprefix("sha256:")

        content = await client.get(f"/v1/artifacts/{pointer['artifact_id']}/content")
        assert content.status_code == 200, content.text
        assert content.content == PAYLOAD
        assert hashlib.sha256(content.content).hexdigest() == pointer["sha256"]
        assert content.headers["content-type"].startswith(MEDIA_TYPE)
        assert content.headers["x-fs2-artifact-sha256"] == pointer["sha256"]
        assert content.headers["x-fs2-artifact-size-bytes"] == str(len(PAYLOAD))
        assert content.headers["cache-control"] == "no-store"
        # No storage location or credential may ever reach the customer.
        assert "storage_key" not in content.text
        assert "X-Amz-Signature" not in str(content.headers)

        operation = await client.get(f"/v1/operations/{reservation['operation_id']}")
        assert operation.json()["status"] == "succeeded"
        assert operation.json()["outcome"] == "artifact_uploaded"

    # Exactly one object was written, at the reserved content address.
    assert len(object_store.written) == 1


@pytest.mark.asyncio
async def test_declared_identity_is_immutable_and_a_mismatch_stores_nothing(registry, cipher, hasher) -> None:
    """Digest, size and media type are bound at reservation, not at write time."""

    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    object_store, _ = _artifact_plane(runtime)
    token = await _token(runtime, principal_id="scientist-a", tenant_id="tenant-a")
    app = create_app(runtime)
    async with app.router.lifespan_context(app), _client(app, token) as client:
        reservation = await _begin(client, key="public-bytes-0002", request=_upload_request())

        wrong_bytes = await _put(client, reservation, PAYLOAD + b"X")
        assert wrong_bytes.status_code == 422
        assert wrong_bytes.json()["error"]["type"] == "artifact_verification_failed"

        wrong_type = await _put(client, reservation, PAYLOAD, media_type="chemical/x-pdb")
        assert wrong_type.status_code == 422

        truncated = await _put(client, reservation, PAYLOAD[:-1])
        assert truncated.status_code == 422

        # Nothing was stored, so the upload cannot be finalized either.
        assert object_store.written == []
        assert object_store.objects == {}
        premature = await client.post(
            f"/v1/scientific-artifacts/uploads/{reservation['upload_id']}:finalize",
            json={"operation_id": reservation["operation_id"]},
        )
        assert premature.status_code == 404
        assert premature.json()["error"]["type"] == "artifact_not_found"

        # The exact declared bytes are accepted, and only those.
        assert (await _put(client, reservation, PAYLOAD)).status_code == 200
        finalized = await client.post(
            f"/v1/scientific-artifacts/uploads/{reservation['upload_id']}:finalize",
            json={"operation_id": reservation["operation_id"]},
        )
        assert finalized.status_code == 200

        # A finalized content address is write-once.
        replaced = await _put(client, reservation, PAYLOAD)
        assert replaced.status_code == 409
        assert replaced.json()["error"]["type"] == "artifact_conflict"


@pytest.mark.asyncio
async def test_a_store_that_rewrites_the_body_is_rejected(registry, cipher, hasher) -> None:
    """The service trusts its own measurement of the persisted object, not the request."""

    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    object_store, _ = _artifact_plane(runtime)
    object_store.rewrite = PAYLOAD + b"tampered"
    token = await _token(runtime, principal_id="scientist-a", tenant_id="tenant-a")
    app = create_app(runtime)
    async with app.router.lifespan_context(app), _client(app, token) as client:
        reservation = await _begin(client, key="public-bytes-0003", request=_upload_request())
        rewritten = await _put(client, reservation, PAYLOAD)
        assert rewritten.status_code == 422
        assert rewritten.json()["error"]["type"] == "artifact_verification_failed"


@pytest.mark.asyncio
async def test_inline_ceiling_is_enforced_and_refers_the_client_to_a_handle(registry, cipher, hasher) -> None:
    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    object_store, _ = _artifact_plane(runtime, max_inline_content_bytes=len(PAYLOAD) - 1)
    token = await _token(runtime, principal_id="scientist-a", tenant_id="tenant-a")
    app = create_app(runtime)
    async with app.router.lifespan_context(app), _client(app, token) as client:
        reservation = await _begin(client, key="public-bytes-0004", request=_upload_request())
        assert reservation["max_content_bytes"] == len(PAYLOAD) - 1
        oversized = await _put(client, reservation, PAYLOAD)
        assert oversized.status_code == 413
        assert oversized.json()["error"]["type"] == "artifact_content_too_large"
        assert object_store.written == []


@pytest.mark.asyncio
async def test_a_foreign_tenant_can_neither_write_nor_read_the_bytes(registry, cipher, hasher) -> None:
    """Tenant isolation holds on the reservation, the bytes and the finalization."""

    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    object_store, _ = _artifact_plane(runtime)
    owner = await _token(runtime, principal_id="scientist-a", tenant_id="tenant-a")
    intruder = await _token(runtime, principal_id="scientist-b", tenant_id="tenant-b")
    app = create_app(runtime)
    async with app.router.lifespan_context(app), _client(app, owner) as client:
        reservation = await _begin(client, key="public-bytes-0005", request=_upload_request())
        assert (await _put(client, reservation, PAYLOAD)).status_code == 200
        finalized = await client.post(
            f"/v1/scientific-artifacts/uploads/{reservation['upload_id']}:finalize",
            json={"operation_id": reservation["operation_id"]},
        )
        assert finalized.status_code == 200
        pointer = finalized.json()

    async with app.router.lifespan_context(app), _client(app, intruder) as other:
        stolen_write = await _put(other, reservation, PAYLOAD)
        assert stolen_write.status_code == 404
        stolen_finalize = await other.post(
            f"/v1/scientific-artifacts/uploads/{reservation['upload_id']}:finalize",
            json={"operation_id": reservation["operation_id"]},
        )
        assert stolen_finalize.status_code == 404
        stolen_bytes = await other.get(f"/v1/artifacts/{pointer['artifact_id']}/content")
        assert stolen_bytes.status_code == 404
        assert PAYLOAD.decode() not in stolen_bytes.text
        stolen_handle = await other.get(f"/v1/artifacts/{pointer['artifact_id']}/download")
        assert stolen_handle.status_code == 404
        stolen_status = await other.get(f"/v1/operations/{reservation['operation_id']}")
        assert stolen_status.status_code == 404
        # The owner's object itself was never touched by the foreign tenant.
        assert len(object_store.written) == 1


@pytest.mark.asyncio
async def test_result_scope_is_required_to_read_artifact_bytes(registry, cipher, hasher) -> None:
    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    _artifact_plane(runtime)
    owner = await _token(runtime, principal_id="scientist-a", tenant_id="tenant-a")
    limited = await runtime.tokens.issue(
        TokenCreate(
            principal_id="scientist-c",
            tenant_id="tenant-a",
            scopes={Scope.INFERENCE_INVOKE, Scope.OPERATIONS_READ},
            models={"protein-design"},
            max_concurrency=1,
        ),
        created_by="test",
    )
    app = create_app(runtime)
    async with app.router.lifespan_context(app), _client(app, owner) as client:
        reservation = await _begin(client, key="public-bytes-0006", request=_upload_request())
        assert (await _put(client, reservation, PAYLOAD)).status_code == 200
        pointer = (
            await client.post(
                f"/v1/scientific-artifacts/uploads/{reservation['upload_id']}:finalize",
                json={"operation_id": reservation["operation_id"]},
            )
        ).json()

    async with app.router.lifespan_context(app), _client(app, str(limited.token)) as weak:
        refused = await weak.get(f"/v1/artifacts/{pointer['artifact_id']}/content")
        assert refused.status_code == 403


@pytest.mark.asyncio
async def test_an_unknown_model_cannot_reserve_an_upload(registry, cipher, hasher) -> None:
    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    _artifact_plane(runtime)
    issued = await runtime.tokens.issue(
        TokenCreate(
            principal_id="scientist-a",
            tenant_id="tenant-a",
            scopes={Scope.INFERENCE_INVOKE, Scope.OPERATIONS_READ, Scope.OPERATIONS_RESULT},
            models={"protein-design", "not-a-scientific-model"},
            max_concurrency=1,
        ),
        created_by="test",
    )
    app = create_app(runtime)
    async with app.router.lifespan_context(app), _client(app, str(issued.token)) as client:
        response = await client.post(
            "/v1/scientific-artifacts/uploads",
            headers={"idempotency-key": "public-bytes-0007"},
            json=_upload_request(model_id="not-a-scientific-model"),
        )
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "scientific_profile_unavailable"


@pytest.mark.asyncio
async def test_absent_bytes_and_absent_artifacts_are_not_found(registry, cipher, hasher) -> None:
    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    _artifact_plane(runtime)
    token = await _token(runtime, principal_id="scientist-a", tenant_id="tenant-a")
    app = create_app(runtime)
    async with app.router.lifespan_context(app), _client(app, token) as client:
        missing = await client.get(f"/v1/artifacts/{uuid4()}/content")
        assert missing.status_code == 404
        unreserved = await client.put(
            f"/v1/scientific-artifacts/uploads/{uuid4()}/content?operation_id={uuid4()}",
            content=PAYLOAD,
            headers={"content-type": MEDIA_TYPE},
        )
        assert unreserved.status_code == 404


@pytest.mark.asyncio
async def test_mcp_offers_the_same_upload_submit_status_result_operations(registry, cipher, hasher) -> None:
    """MCP reaches every public operation, including the bytes themselves."""

    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    _artifact_plane(runtime)
    token = await _token(runtime, principal_id="scientist-a", tenant_id="tenant-a")
    server = build_mcp_server(runtime)
    access = await PATTokenVerifier(runtime).verify_token(token)
    assert access is not None
    context = Context(mcp_server=server, subscriptions=server._subscriptions)  # type: ignore[attr-defined]

    parity = {
        "begin_scientific_artifact_upload",
        "put_scientific_artifact_bytes",
        "finalize_scientific_artifact_upload",
        "submit_scientific_run",
        "get_scientific_status",
        "get_scientific_result",
        "get_scientific_artifact",
        "download_scientific_artifact",
        "read_scientific_artifact_bytes",
    }
    listed = {tool.name for tool in server._tool_manager.list_tools()}  # type: ignore[attr-defined]
    assert parity <= listed
    assert parity <= CORE_TOOLS

    auth_token = auth_context_var.set(AuthenticatedUser(access))
    try:
        reservation = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "begin_scientific_artifact_upload",
            {
                "model_id": "protein-design",
                "sha256": digest(PAYLOAD).removeprefix("sha256:"),
                "size_bytes": len(PAYLOAD),
                "media_type": MEDIA_TYPE,
                "idempotency_key": "mcp-public-bytes-0001",
            },
            context,
            convert_result=False,
        )
        receipt = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "put_scientific_artifact_bytes",
            {
                "operation_id": reservation["operation_id"],
                "upload_id": reservation["upload_id"],
                "content_base64": base64.b64encode(PAYLOAD).decode("ascii"),
            },
            context,
            convert_result=False,
        )
        assert receipt["sha256"] == digest(PAYLOAD).removeprefix("sha256:")
        pointer = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "finalize_scientific_artifact_upload",
            {"operation_id": reservation["operation_id"], "upload_id": reservation["upload_id"]},
            context,
            convert_result=False,
        )
        read = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "read_scientific_artifact_bytes",
            {"artifact_id": pointer["artifact_id"]},
            context,
            convert_result=False,
        )
        assert base64.b64decode(read["content_base64"]) == PAYLOAD
        assert read["artifact"] == pointer
        status = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "get_operation",
            {"operation_id": reservation["operation_id"]},
            context,
            convert_result=False,
        )
        assert status["outcome"] == "artifact_uploaded"
    finally:
        auth_context_var.reset(auth_token)


@pytest.mark.asyncio
async def test_mcp_bytes_refuse_a_mismatch_and_a_foreign_tenant(registry, cipher, hasher) -> None:
    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    object_store, _ = _artifact_plane(runtime)
    owner = await _token(runtime, principal_id="scientist-a", tenant_id="tenant-a")
    intruder = await _token(runtime, principal_id="scientist-b", tenant_id="tenant-b")
    server = build_mcp_server(runtime)
    context = Context(mcp_server=server, subscriptions=server._subscriptions)  # type: ignore[attr-defined]
    owner_access = await PATTokenVerifier(runtime).verify_token(owner)
    intruder_access = await PATTokenVerifier(runtime).verify_token(intruder)
    assert owner_access is not None and intruder_access is not None

    auth_token = auth_context_var.set(AuthenticatedUser(owner_access))
    try:
        reservation = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "begin_scientific_artifact_upload",
            {
                "model_id": "protein-design",
                "sha256": digest(PAYLOAD).removeprefix("sha256:"),
                "size_bytes": len(PAYLOAD),
                "media_type": MEDIA_TYPE,
                "idempotency_key": "mcp-public-bytes-0002",
            },
            context,
            convert_result=False,
        )
        with pytest.raises(Exception, match="artifact"):
            await server._tool_manager.call_tool(  # type: ignore[attr-defined]
                "put_scientific_artifact_bytes",
                {
                    "operation_id": reservation["operation_id"],
                    "upload_id": reservation["upload_id"],
                    "content_base64": base64.b64encode(PAYLOAD + b"X").decode("ascii"),
                },
                context,
                convert_result=False,
            )
        assert object_store.written == []
        with pytest.raises(Exception, match="base64"):
            await server._tool_manager.call_tool(  # type: ignore[attr-defined]
                "put_scientific_artifact_bytes",
                {
                    "operation_id": reservation["operation_id"],
                    "upload_id": reservation["upload_id"],
                    "content_base64": "not base64 at all",
                },
                context,
                convert_result=False,
            )
        assert (
            await server._tool_manager.call_tool(  # type: ignore[attr-defined]
                "put_scientific_artifact_bytes",
                {
                    "operation_id": reservation["operation_id"],
                    "upload_id": reservation["upload_id"],
                    "content_base64": base64.b64encode(PAYLOAD).decode("ascii"),
                },
                context,
                convert_result=False,
            )
        )["size_bytes"] == len(PAYLOAD)
        pointer = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "finalize_scientific_artifact_upload",
            {"operation_id": reservation["operation_id"], "upload_id": reservation["upload_id"]},
            context,
            convert_result=False,
        )
    finally:
        auth_context_var.reset(auth_token)

    auth_token = auth_context_var.set(AuthenticatedUser(intruder_access))
    try:
        with pytest.raises(Exception):  # noqa: B017 - the SDK collapses the failure class
            await server._tool_manager.call_tool(  # type: ignore[attr-defined]
                "read_scientific_artifact_bytes",
                {"artifact_id": pointer["artifact_id"]},
                context,
                convert_result=False,
            )
        with pytest.raises(Exception):  # noqa: B017 - the SDK collapses the failure class
            await server._tool_manager.call_tool(  # type: ignore[attr-defined]
                "put_scientific_artifact_bytes",
                {
                    "operation_id": reservation["operation_id"],
                    "upload_id": reservation["upload_id"],
                    "content_base64": base64.b64encode(PAYLOAD).decode("ascii"),
                },
                context,
                convert_result=False,
            )
    finally:
        auth_context_var.reset(auth_token)


@pytest.mark.asyncio
async def test_mcp_refuses_to_return_bytes_above_the_inline_ceiling(registry, cipher, hasher) -> None:
    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    _artifact_plane(runtime)
    token = await _token(runtime, principal_id="scientist-a", tenant_id="tenant-a")
    app = create_app(runtime)
    async with app.router.lifespan_context(app), _client(app, token) as client:
        reservation = await _begin(client, key="public-bytes-0008", request=_upload_request())
        assert (await _put(client, reservation, PAYLOAD)).status_code == 200
        pointer = (
            await client.post(
                f"/v1/scientific-artifacts/uploads/{reservation['upload_id']}:finalize",
                json={"operation_id": reservation["operation_id"]},
            )
        ).json()

    # Lower the ceiling under the already-published artifact.
    assert runtime.artifact_service is not None
    runtime.artifact_service._max_inline_content_bytes = len(PAYLOAD) - 1  # type: ignore[attr-defined]
    server = build_mcp_server(runtime)
    context = Context(mcp_server=server, subscriptions=server._subscriptions)  # type: ignore[attr-defined]
    access = await PATTokenVerifier(runtime).verify_token(token)
    assert access is not None
    auth_token = auth_context_var.set(AuthenticatedUser(access))
    try:
        with pytest.raises(Exception, match="ceiling"):
            await server._tool_manager.call_tool(  # type: ignore[attr-defined]
                "read_scientific_artifact_bytes",
                {"artifact_id": pointer["artifact_id"]},
                context,
                convert_result=False,
            )
    finally:
        auth_context_var.reset(auth_token)

    # The gateway byte path still serves the artifact it published.
    async with app.router.lifespan_context(app), _client(app, token) as client:
        served = await client.get(f"/v1/artifacts/{pointer['artifact_id']}/content")
        assert served.status_code == 200
        assert served.content == PAYLOAD


@pytest.mark.asyncio
async def test_upload_surface_reports_disabled_rather_than_failing_late(registry, cipher, hasher) -> None:
    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    runtime.artifact_service = None
    runtime.scientific_input_uploads = None
    token = await _token(runtime, principal_id="scientist-a", tenant_id="tenant-a")
    app = create_app(runtime)
    async with app.router.lifespan_context(app), _client(app, token) as client:
        begun = await client.post(
            "/v1/scientific-artifacts/uploads",
            headers={"idempotency-key": "public-bytes-0009"},
            json=_upload_request(),
        )
        assert begun.status_code == 503
        assert begun.json()["error"]["type"] == "scientific_artifact_upload_unavailable"
        written = await client.put(
            f"/v1/scientific-artifacts/uploads/{uuid4()}/content?operation_id={uuid4()}",
            content=PAYLOAD,
            headers={"content-type": MEDIA_TYPE},
        )
        assert written.status_code == 503
        read = await client.get(f"/v1/artifacts/{uuid4()}/content")
        assert read.status_code == 404


@pytest.mark.asyncio
async def test_operation_identity_must_match_the_upload_identity(registry, cipher, hasher) -> None:
    """A reserved upload cannot be filled through another operation of the same tenant."""

    runtime, _, _, _, _ = scientific_runtime(registry, cipher, hasher)
    object_store, _ = _artifact_plane(runtime)
    token = await _token(runtime, principal_id="scientist-a", tenant_id="tenant-a")
    app = create_app(runtime)
    async with app.router.lifespan_context(app), _client(app, token) as client:
        first = await _begin(client, key="public-bytes-0010", request=_upload_request())
        second = await _begin(client, key="public-bytes-0011", request=_upload_request(PAYLOAD + b"\n"))
        crossed = await client.put(
            f"/v1/scientific-artifacts/uploads/{first['upload_id']}/content?operation_id={second['operation_id']}",
            content=PAYLOAD,
            headers={"content-type": MEDIA_TYPE},
        )
        assert crossed.status_code == 404
        assert object_store.written == []


def test_every_advertised_content_path_is_a_real_public_route() -> None:
    """The discoverable byte path must be exactly the route the app serves."""

    operation_id, upload_id = uuid4(), uuid4()
    advertised = content_path(operation_id, upload_id)
    assert advertised == f"/v1/scientific-artifacts/uploads/{upload_id}/content?operation_id={operation_id}"
    assert UUID(advertised.split("/uploads/")[1].split("/content")[0]) == upload_id
