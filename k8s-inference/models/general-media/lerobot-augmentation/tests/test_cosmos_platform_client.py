from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "src"))

from fs2_lerobot_augmentation.contracts import AugmentationRequest  # noqa: E402
from fs2_lerobot_augmentation.cosmos import CosmosClient  # noqa: E402


def test_cosmos_client_uses_attributed_platform_operations_and_artifacts(
    tmp_path: Path, monkeypatch: Any
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
    invocation: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.method, request.url.path))
        if (
            request.method == "POST"
            and request.url.path
            == "/internal/scientific-workloads/cosmos/v1/scientific-artifacts/uploads"
        ):
            assert request.headers["idempotency-key"].startswith("lr-")
            return httpx.Response(
                201,
                json={
                    "operation_id": upload_operation,
                    "upload_id": upload_id,
                    "content_path": f"/internal/scientific-workloads/cosmos/v1/scientific-artifacts/uploads/{upload_id}/content?operation_id={upload_operation}",
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
        if request.method == "PUT" and request.url.path.endswith(
            f"/{upload_id}/content"
        ):
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
        if request.method == "POST" and request.url.path.endswith(
            f"/{upload_id}:finalize"
        ):
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
            and request.url.path
            == "/internal/scientific-workloads/cosmos/v1/models/cosmos3-nano:invoke"
        ):
            invocation.update(json.loads(request.content))
            assert request.headers["idempotency-key"].startswith("lr-")
            return httpx.Response(
                202, json={"id": inference_operation, "status": "queued"}
            )
        if (
            request.method == "GET"
            and request.url.path
            == f"/internal/scientific-workloads/cosmos/v1/operations/{inference_operation}"
        ):
            return httpx.Response(
                200, json={"id": inference_operation, "status": "succeeded"}
            )
        if (
            request.method == "GET"
            and request.url.path
            == f"/internal/scientific-workloads/cosmos/v1/operations/{inference_operation}/result"
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
                        "media_type": "video/mp4",
                        "compression": "none",
                    },
                },
            )
        if (
            request.method == "GET"
            and request.url.path
            == f"/internal/scientific-workloads/cosmos/v1/artifacts/{output_artifact}/content"
        ):
            return httpx.Response(
                200, content=output_bytes, headers={"content-type": "video/mp4"}
            )
        return httpx.Response(404)

    parsed = AugmentationRequest.parse(
        json.loads((ROOT / "fixtures" / "fixture-request.json").read_text())
    )
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
    result = client.generate(
        reference=source,
        output=output,
        prompt="Change only the lighting.",
        width=256,
        height=256,
        frames=16,
        fps=8,
        seed=20260915,
        augmentation=parsed.augmentation,
        unit_key="v0/e0/front/s20260915",
        cancelled=lambda: False,
    )
    assert result.operation_id == inference_operation
    assert output.read_bytes() == output_bytes
    assert invocation["operation"] == "generate-media"
    assert invocation["payload"]["mode"] == "video-to-video"
    assert invocation["payload"]["input_reference"]["artifact_id"] == input_artifact
    assert all(
        not path.startswith("/internal/scientific-workloads/cosmos/v1/videos")
        for _, path in observed
    )
