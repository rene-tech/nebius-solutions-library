from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "src"))

from fs2_lerobot_augmentation.contracts import AugmentationRequest  # noqa: E402
from fs2_lerobot_augmentation.cosmos import CosmosClient, CosmosError, _operation_id  # noqa: E402


@pytest.mark.parametrize("admission_shape", ["bare", "envelope", "invalid-id", "invalid-json"])
def test_cosmos_client_uses_attributed_platform_operations_and_artifacts(
    tmp_path: Path, monkeypatch: Any, admission_shape: str
) -> None:
    source_bytes = b"\x00\x00\x00\x18ftypisom-source"
    output_bytes = b"\x00\x00\x00\x18ftypisom-generated"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    output = tmp_path / "generated.mp4"
    upload_operation = "00000000-0000-4000-8000-000000000011"
    upload_id = "00000000-0000-4000-8000-000000000012"
    input_artifact = "00000000-0000-4000-8000-000000000013"
    inference_operation = "00000000-0000-4000-8000-000000000021"
    output_artifact = "00000000-0000-4000-8000-000000000022"
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    output_sha = hashlib.sha256(output_bytes).hexdigest()
    observed: list[tuple[str, str]] = []
    invocations: list[dict[str, Any]] = []
    inference_ids: list[str] = []
    invocation_keys: list[str] = []
    upload_finalized = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_finalized
        observed.append((request.method, request.url.path))
        if (
            request.method == "POST"
            and request.url.path == "/internal/scientific-workloads/cosmos/v1/scientific-artifacts/uploads"
        ):
            assert request.headers["idempotency-key"].startswith("lr-")
            return httpx.Response(
                201,
                json={
                    "operation_id": upload_operation,
                    "upload_id": upload_id,
                    "content_path": (
                        f"/internal/scientific-workloads/cosmos/v1/scientific-artifacts/uploads/{upload_id}"
                        f"/content?operation_id={upload_operation}"
                    ),
                    "max_content_bytes": 1024,
                    "handle": {
                        "method": "PUT",
                        "url": "https://objects.invalid",
                        "expires_at": "2026-09-15T20:00:00Z",
                        "write_once": True,
                        "headers": {},
                    },
                },
            )
        if request.method == "PUT" and request.url.path.endswith(f"/{upload_id}/content"):
            if upload_finalized:
                return httpx.Response(409, json={"detail": "a finalized upload cannot accept new bytes"})
            assert request.read() == source_bytes
            return httpx.Response(
                200,
                json={
                    "operation_id": upload_operation,
                    "upload_id": upload_id,
                    "sha256": source_sha,
                    "size_bytes": len(source_bytes),
                    "media_type": "video/mp4",
                    "finalized": False,
                },
            )
        if request.method == "POST" and request.url.path.endswith(f"/{upload_id}:finalize"):
            upload_finalized = True
            return httpx.Response(
                200,
                json={
                    "artifact_id": input_artifact,
                    "sha256": source_sha,
                    "size_bytes": len(source_bytes),
                    "media_type": "video/mp4",
                    "compression": "none",
                },
            )
        if (
            request.method == "POST"
            and request.url.path == "/internal/scientific-workloads/cosmos/v1/models/cosmos3-nano:invoke"
        ):
            invocations.append(json.loads(request.content))
            invocation_keys.append(request.headers["idempotency-key"])
            assert request.headers["idempotency-key"].startswith("lr-")
            operation_id = str(UUID(int=UUID(inference_operation).int + len(invocations) - 1))
            inference_ids.append(operation_id)
            operation = {"id": operation_id, "operation": "generate-media", "status": "queued"}
            if admission_shape == "invalid-json":
                return httpx.Response(202, content=b"not-json")
            if admission_shape == "invalid-id":
                operation["id"] = "invalid"
            return httpx.Response(202, json={"operation": operation} if admission_shape == "envelope" else operation)
        if request.method == "GET" and request.url.path.endswith(f"/operations/{upload_operation}"):
            return httpx.Response(
                200,
                json={
                    "id": upload_operation,
                    "operation": "upload",
                    "status": "succeeded" if upload_finalized else "queued",
                },
            )
        if request.method == "GET" and any(
            request.url.path.endswith(f"/operations/{operation_id}") for operation_id in inference_ids
        ):
            polls = sum(method == "GET" and path == request.url.path for method, path in observed)
            return httpx.Response(
                200,
                json={
                    "id": request.url.path.rsplit("/", 1)[-1],
                    "operation": "generate-media",
                    "status": "activating" if polls == 1 else "succeeded",
                },
            )
        if request.method == "GET" and any(
            request.url.path.endswith(f"/operations/{operation_id}/result") for operation_id in inference_ids
        ):
            return httpx.Response(
                200,
                json={
                    "schema": "fs2-serve.nebius.ai/operation-artifact-result/v1",
                    "content_type": "video/mp4",
                    "artifact": {
                        "artifact_id": output_artifact,
                        "sha256": output_sha,
                        "size_bytes": len(output_bytes),
                        "media_type": "application/octet-stream",
                        "compression": "none",
                    },
                },
            )
        if (
            request.method == "GET"
            and request.url.path == f"/internal/scientific-workloads/cosmos/v1/artifacts/{output_artifact}/content"
        ):
            return httpx.Response(200, content=output_bytes, headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    parsed = AugmentationRequest.parse(json.loads((ROOT / "fixtures" / "fixture-request.json").read_text()))
    client = CosmosClient(
        "https://platform.invalid",
        workload_capability="test-secret",
        parent_operation_id="00000000-0000-4000-8000-000000000001",
    )
    monkeypatch.setattr(
        client,
        "_client",
        lambda: httpx.Client(
            base_url="https://platform.invalid",
            headers={"Authorization": "Bearer test-secret"},
            transport=httpx.MockTransport(handler),
        ),
    )
    monkeypatch.setattr("fs2_lerobot_augmentation.cosmos.time.sleep", lambda seconds: None)
    if admission_shape.startswith("invalid"):
        with pytest.raises(CosmosError) as caught:
            client.generate(
                reference=source,
                output=output,
                prompt="Lighting",
                width=256,
                height=256,
                frames=16,
                fps=8,
                seed=20260915,
                augmentation=parsed.augmentation,
                unit_key="v0/e0/front/s20260915",
                cancelled=lambda: False,
            )
        assert caught.value.code == "PLATFORM_RESPONSE_INVALID" and not caught.value.retryable
        assert sum(method == "POST" and path.endswith(":invoke") for method, path in observed) == 1
        assert not any(method == "GET" and not path.endswith(upload_operation) for method, path in observed)
        assert not output.exists()
        return
    for variant in range(2):
        result = client.generate(
            reference=source,
            output=tmp_path / f"generated-{variant}.mp4",
            prompt="Change only the lighting.",
            width=256,
            height=256,
            frames=16,
            fps=8,
            seed=20260915 + variant,
            augmentation=parsed.augmentation,
            unit_key=f"v{variant}/e0/front/s{20260915 + variant}",
            cancelled=lambda: False,
        )
        assert result.operation_id == inference_ids[variant]
        assert result.video_path.read_bytes() == output_bytes
    assert len(set(inference_ids)) == len(set(invocation_keys)) == 2
    assert [invocation["payload"]["seed"] for invocation in invocations] == [20260915, 20260916]
    assert all(invocation["operation"] == "generate-media" for invocation in invocations)
    assert all(invocation["payload"]["mode"] == "video-to-video" for invocation in invocations)
    assert all(invocation["payload"]["input_reference"]["artifact_id"] == input_artifact for invocation in invocations)
    assert sum(method == "POST" and path.endswith("/uploads") for method, path in observed) == 2
    assert sum(method == "PUT" for method, path in observed) == 1
    assert sum(method == "POST" and path.endswith(":finalize") for method, path in observed) == 2
    assert sum(method == "POST" and path.endswith(":invoke") for method, path in observed) == 2
    assert all(
        sum(method == "GET" and path.endswith(operation_id) for method, path in observed) == 2
        for operation_id in inference_ids
    )
    assert all(not path.startswith("/internal/scientific-workloads/cosmos/v1/videos") for _, path in observed)


@pytest.mark.parametrize("envelope", [False, True])
def test_operation_parser_keeps_bare_upload_id_and_nested_operations(envelope):
    operation_id = "00000000-0000-4000-8000-000000000021"
    body = {"operation_id": operation_id}
    assert _operation_id({"operation": body} if envelope else body) == operation_id


@pytest.mark.parametrize(
    ("state", "invalid_field", "put_status", "error_code"),
    [
        ("succeeded", None, 200, None),
        ("succeeded", "sha256", 200, "PLATFORM_RESPONSE_INVALID"),
        ("succeeded", "size_bytes", 200, "PLATFORM_RESPONSE_INVALID"),
        ("succeeded", "media_type", 200, "PLATFORM_RESPONSE_INVALID"),
        ("succeeded", "status_id", 200, "PLATFORM_RESPONSE_INVALID"),
        ("queued", None, 409, "PLATFORM_UPSTREAM_ERROR"),
        ("failed", None, 200, "PLATFORM_UPSTREAM_ERROR"),
        ("cancelled", None, 200, "PLATFORM_UPSTREAM_ERROR"),
        ("expired", None, 200, "PLATFORM_UPSTREAM_ERROR"),
        ("running", None, 200, "PLATFORM_RESPONSE_INVALID"),
        (None, None, 200, "PLATFORM_RESPONSE_INVALID"),
        ({"invalid": "state"}, None, 200, "PLATFORM_RESPONSE_INVALID"),
    ],
)
def test_upload_replay_uses_durable_status_and_validates_immutable_identity(
    tmp_path: Path, state: Any, invalid_field: str | None, put_status: int, error_code: str | None
) -> None:
    source_bytes = b"\x00\x00\x00\x18ftypisom-source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    upload_operation = "00000000-0000-4000-8000-000000000011"
    upload_id = "00000000-0000-4000-8000-000000000012"
    artifact: dict[str, Any] = {
        "artifact_id": "00000000-0000-4000-8000-000000000013",
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "size_bytes": len(source_bytes),
        "media_type": "video/mp4",
        "compression": "none",
    }
    if invalid_field is not None and invalid_field != "status_id":
        artifact[invalid_field] = {"sha256": "0" * 64, "size_bytes": 999, "media_type": "image/png"}[invalid_field]
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/uploads"):
            # The actual begin DTO is identical for new/replayed uploads and
            # contains operation_id, not a fabricated nested state/artifact.
            return httpx.Response(
                201,
                json={
                    "operation_id": upload_operation,
                    "upload_id": upload_id,
                    "content_path": (
                        f"/internal/scientific-workloads/cosmos/v1/scientific-artifacts/uploads/{upload_id}"
                        f"/content?operation_id={upload_operation}"
                    ),
                    "max_content_bytes": 1024,
                    "handle": {
                        "method": "PUT",
                        "url": "https://objects.invalid",
                        "write_once": True,
                        "expires_at": "2026-09-18T12:00:00Z",
                        "headers": {},
                    },
                },
            )
        if request.method == "GET" and request.url.path.endswith(upload_operation):
            return httpx.Response(
                200,
                json={
                    "id": upload_id if invalid_field == "status_id" else upload_operation,
                    "operation": "upload",
                    "status": state,
                },
            )
        if request.method == "PUT":
            return httpx.Response(put_status, json={"detail": "conflict"})
        if request.method == "POST" and request.url.path.endswith(":finalize"):
            assert json.loads(request.content) == {"operation_id": upload_operation}
            return httpx.Response(200, json=artifact)
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    # Fresh client has no in-memory record of the earlier finalized upload.
    cosmos = CosmosClient(
        "https://platform.invalid",
        workload_capability="test-secret",
        parent_operation_id="00000000-0000-4000-8000-000000000001",
    )
    with httpx.Client(base_url="https://platform.invalid", transport=httpx.MockTransport(handler)) as client:
        if error_code is None:
            assert cosmos._upload_reference(client, source) == artifact
        else:
            with pytest.raises(CosmosError) as caught:
                cosmos._upload_reference(client, source)
            assert caught.value.code == error_code and not caught.value.retryable
    assert sum(method == "PUT" for method, _ in calls) == int(state == "queued")
    expected_finalizes = int(state == "succeeded" and invalid_field != "status_id")
    assert sum(path.endswith(":finalize") for _, path in calls) == expected_finalizes
