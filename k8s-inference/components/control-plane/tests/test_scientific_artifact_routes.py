"""Route-level authorization, projection and error-mapping tests.

The routes are exercised against the real service and the in-memory repository
rather than a stub, so the assertions cover the same code the deployment runs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_scientific_artifacts import ALLOWED_MEDIA_TYPES, FakeObjectStore, digest, execution_identity
from test_scientific_artifacts import scheduling_snapshot as snapshot

from fs2_serve.models import Principal, Scope
from fs2_serve.scientific_artifact_routes import scientific_artifact_router
from fs2_serve.scientific_artifacts import MemoryArtifactRepository, ScientificArtifactService

NOW = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)
TENANT = "tenant-a"
WRITE_SCOPES = frozenset({str(Scope.ARTIFACTS_WRITE), str(Scope.OPERATIONS_RESULT)})


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class Harness:
    def __init__(self, scopes: frozenset[str] = WRITE_SCOPES, tenant: str = TENANT) -> None:
        self.repository = MemoryArtifactRepository()
        self.store = FakeObjectStore()
        self.service = ScientificArtifactService(
            repository=self.repository,
            object_store=self.store,
            allowed_media_types=ALLOWED_MEDIA_TYPES,
            clock=lambda: NOW,
        )
        self.operation_id = uuid4()
        self.scopes = scopes
        self.tenant = tenant

    async def principal(self) -> Principal:
        return Principal(
            token_id=uuid4(),
            token_prefix="fst_test",
            principal_id="principal-a",
            tenant_id=self.tenant,
            scopes=self.scopes,
            models=frozenset({"proteina-complexa"}),
        )

    def client(self) -> TestClient:
        app = FastAPI()

        @app.exception_handler(PermissionError)
        async def permission_error(_: Request, __: PermissionError) -> JSONResponse:
            """Mirror the application handler so scope failures surface as 403."""

            return JSONResponse(
                status_code=403,
                content={"error": {"type": "permission_denied", "message": "request is outside token policy"}},
            )

        app.include_router(scientific_artifact_router(service=self.service, principal_dependency=self.principal))
        return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
async def harness():
    item = Harness()
    await item.repository.register_operation(item.operation_id, tenant_id=TENANT)
    return item


def open_attempt(client: TestClient, operation_id: UUID, *, shard: str | None = "candidate-0001") -> UUID:
    attempt_id = uuid4()
    response = client.post(
        "/internal/scientific-artifacts/attempts",
        json={
            "attempt_id": str(attempt_id),
            "operation_id": str(operation_id),
            "stage_id": "design",
            "shard_id": shard,
            "attempt_number": 1,
            "admission": {
                "resolved_pool_id": "gpu-preemptible",
                "admitted_resource_flavor": "gpu-preemptible",
                "accelerator_resource_name": "nvidia.com/gpu",
                "accelerator_count": 1,
                "admitted_at": iso(NOW),
            },
            "kueue_workload_uid": "kueue-1",
            "k8s_job_uid": "job-1",
            "started_at": iso(NOW),
        },
    )
    assert response.status_code == 201, response.text
    return attempt_id


def publish(
    harness: Harness,
    client: TestClient,
    attempt_id: UUID,
    payload: bytes,
    *,
    media_type: str = "chemical/x-pdb",
    direction: str = "output",
) -> str:
    upload_id = uuid4()
    begin = client.post(
        "/internal/scientific-artifacts/uploads",
        json={
            "upload_id": str(upload_id),
            "attempt_id": str(attempt_id),
            "operation_id": str(harness.operation_id),
            "direction": direction,
            "sha256": digest(payload).removeprefix("sha256:"),
            "size_bytes": len(payload),
            "media_type": media_type,
        },
    )
    assert begin.status_code == 201, begin.text
    body = begin.json()
    assert set(body) == {"upload_id", "handle"}
    # The presigned URL necessarily addresses the object, so the leak check
    # applies to the response data rather than to the bearer handle itself.
    assert "tenant_id" not in json.dumps({key: item for key, item in body.items() if key != "handle"})
    key = next(iter(harness.store.issued[-1:])).url.split("/bucket/")[1].split("?")[0]
    harness.store.put(key, payload, media_type)
    finalize = client.post(
        f"/internal/scientific-artifacts/uploads/{upload_id}:finalize",
        json={"operation_id": str(harness.operation_id)},
    )
    assert finalize.status_code == 200, finalize.text
    body = finalize.json()
    assert set(body) <= {"artifact_id", "sha256", "size_bytes", "media_type", "compression"}
    return str(body["artifact_id"])


async def test_full_flow_publishes_a_canonical_result_without_leaking_internals(harness) -> None:
    with harness.client() as client:
        attempt_id = open_attempt(client, harness.operation_id)
        inputs = publish(
            harness,
            client,
            attempt_id,
            b'{"sequence":"MKT"}',
            media_type="application/vnd.fs2.scientific-manifest+json",
            direction="input",
        )
        outputs = publish(harness, client, attempt_id, b"ATOM  CA  ALA A   1")

        closed = client.post(
            f"/internal/scientific-artifacts/attempts/{attempt_id}:close",
            json={
                "operation_id": str(harness.operation_id),
                "status": "succeeded",
                "completed_at": iso(NOW + timedelta(minutes=5)),
                "pod_uids": ["pod-1"],
                "gpu_uuids": ["GPU-1"],
            },
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["status"] == "succeeded"

        commit = client.post(
            "/internal/scientific-artifacts/stages:commit",
            json={
                "operation_id": str(harness.operation_id),
                "stage_id": "design",
                "attempt_ids": [str(attempt_id)],
                "entries": [
                    {
                        "name": "designed-backbone",
                        "semantic_type": "protein.structure/v1",
                        "artifact_id": outputs,
                    }
                ],
                "validation_digest": "9" * 64,
                "semantic_valid": True,
                "committed_at": iso(NOW + timedelta(minutes=6)),
                "validated_at": iso(NOW + timedelta(minutes=6)),
            },
        )
        assert commit.status_code == 201, commit.text
        assert commit.json()["manifest"]["schema"] == "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"

        read = client.get(f"/internal/scientific-artifacts/operations/{harness.operation_id}/stages/design:commit")
        assert read.status_code == 200
        assert read.json()["manifest_digest"] == commit.json()["manifest_digest"]

        published = client.post(
            f"/internal/scientific-artifacts/operations/{harness.operation_id}:result",
            json={
                "terminal_status": "succeeded",
                "submitted_at": iso(NOW),
                "completed_at": iso(NOW + timedelta(minutes=5)),
                "execution_identity": execution_identity(),
                "scheduling_snapshot": snapshot(),
                "input_manifest_artifact_id": inputs,
                "output_manifest_artifact_id": outputs,
                "validator_id": "proteina-complexa-validator",
                "validation_status": "passed",
                "validation_receipt_sha256": "2" * 64,
            },
        )
        assert published.status_code == 201, published.text
        document = published.json()["result"]
        assert document["schema"] == "fs2-serve.nebius.ai/scientific-run-result/v1"
        # tenant_queue is a canonical scheduling field; the internal tenant
        # identity and the storage location are what must never appear.
        assert "tenant_id" not in json.dumps(document)
        assert "storage_key" not in published.text

        reread = client.get(f"/internal/scientific-artifacts/operations/{harness.operation_id}:result")
        assert reread.status_code == 200
        assert reread.json()["result_digest"] == published.json()["result_digest"]

        events = client.get(f"/internal/scientific-artifacts/operations/{harness.operation_id}/events")
        assert events.status_code == 200
        kinds = [event["event_type"] for event in events.json()["events"]]
        assert kinds == [
            "attempt_opened",
            "upload_begun",
            "artifact_finalized",
            "upload_begun",
            "artifact_finalized",
            "attempt_closed",
            "stage_committed",
            "result_committed",
        ]
        assert "storage_key" not in events.text


async def test_writes_require_the_artifacts_write_scope(harness) -> None:
    harness.scopes = frozenset({str(Scope.OPERATIONS_RESULT)})
    with harness.client() as client:
        refused = client.post(
            "/internal/scientific-artifacts/attempts",
            json={
                "attempt_id": str(uuid4()),
                "operation_id": str(harness.operation_id),
                "stage_id": "design",
                "attempt_number": 1,
                "started_at": iso(NOW),
            },
        )
    assert refused.status_code == 403


async def test_reads_require_the_operations_result_scope(harness) -> None:
    harness.scopes = frozenset({str(Scope.ARTIFACTS_WRITE)})
    with harness.client() as client:
        refused = client.get(f"/internal/scientific-artifacts/operations/{harness.operation_id}:result")
    assert refused.status_code == 403


async def test_the_request_body_can_never_choose_a_tenant(harness) -> None:
    with harness.client() as client:
        rejected = client.post(
            "/internal/scientific-artifacts/attempts",
            json={
                "attempt_id": str(uuid4()),
                "operation_id": str(harness.operation_id),
                "tenant_id": "tenant-b",
                "stage_id": "design",
                "attempt_number": 1,
                "started_at": iso(NOW),
            },
        )
    assert rejected.status_code == 422


async def test_another_tenant_sees_a_not_found_rather_than_a_forbidden(harness) -> None:
    with harness.client() as client:
        attempt_id = open_attempt(client, harness.operation_id)
        artifact_id = publish(harness, client, attempt_id, b"ATOM  CA")
    foreign = Harness(tenant="tenant-b")
    foreign.service = harness.service
    foreign.operation_id = harness.operation_id
    with foreign.client() as client:
        missing = client.get(f"/internal/scientific-artifacts/{artifact_id}:download")
    assert missing.status_code == 404
    assert missing.json()["detail"]["type"] == "artifact_not_found"


async def test_domain_failures_map_onto_stable_public_codes(harness) -> None:
    with harness.client() as client:
        unknown = client.post(
            "/internal/scientific-artifacts/uploads",
            json={
                "upload_id": str(uuid4()),
                "attempt_id": str(uuid4()),
                "operation_id": str(harness.operation_id),
                "direction": "output",
                "sha256": "a" * 64,
                "size_bytes": 4,
                "media_type": "chemical/x-pdb",
            },
        )
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["type"] == "artifact_not_found"

        attempt_id = open_attempt(client, harness.operation_id)
        refused = client.post(
            "/internal/scientific-artifacts/uploads",
            json={
                "upload_id": str(uuid4()),
                "attempt_id": str(attempt_id),
                "operation_id": str(harness.operation_id),
                "direction": "output",
                "sha256": "a" * 64,
                "size_bytes": 4,
                "media_type": "application/x-msdownload",
            },
        )
        assert refused.status_code == 422
        assert refused.json()["detail"]["type"] == "artifact_policy_rejected"

        absent_stage = client.get(
            f"/internal/scientific-artifacts/operations/{harness.operation_id}/stages/score:commit"
        )
        assert absent_stage.status_code == 404


async def test_download_returns_bearer_material_only_to_an_authorized_reader(harness) -> None:
    with harness.client() as client:
        attempt_id = open_attempt(client, harness.operation_id)
        artifact_id = publish(harness, client, attempt_id, b"ATOM  CA")
        response = client.get(f"/internal/scientific-artifacts/{artifact_id}:download")
    assert response.status_code == 200
    body = response.json()
    assert body["handle"]["method"] == "GET"
    assert body["handle"]["write_once"] is False
    assert set(body) == {"artifact", "handle"}
    assert "tenant_id" not in json.dumps(body["artifact"])
    assert set(body["artifact"]) <= {"artifact_id", "sha256", "size_bytes", "media_type", "compression"}
