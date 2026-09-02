from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fs2_serve.models import Principal
from fs2_serve.scientific_artifact_routes import scientific_artifact_router
from fs2_serve.scientific_artifacts import (
    ArtifactAccess,
    ArtifactDirection,
    ArtifactDownload,
    ArtifactRecord,
    BeginArtifactUpload,
    BeginUploadResult,
    EphemeralHandle,
    FinalizeArtifactUpload,
    TerminalResultDraft,
    TerminalResultManifest,
    UploadIntent,
    artifact_storage_key,
)

NOW = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)


class StubControllerService:
    def __init__(self) -> None:
        self.operation_id = uuid4()
        self.artifact_id = uuid4()
        self.digest = "sha256:" + "a" * 64
        self.storage_key = artifact_storage_key(
            tenant_id="tenant-a",
            operation_id=self.operation_id,
            attempt=1,
            direction=ArtifactDirection.OUTPUT,
            digest=self.digest,
        )
        self.artifact = ArtifactRecord(
            artifact_id=self.artifact_id,
            operation_id=self.operation_id,
            tenant_id="tenant-a",
            attempt=1,
            direction=ArtifactDirection.OUTPUT,
            digest=self.digest,
            size_bytes=7,
            media_type="application/json",
            storage_key=self.storage_key,
            access=ArtifactAccess(),
            created_at=NOW,
        )

    async def begin_upload(self, request: BeginArtifactUpload, *, handle_ttl=None) -> BeginUploadResult:
        assert request.tenant_id == "tenant-a"
        return BeginUploadResult(
            upload=UploadIntent(
                **request.model_dump(),
                storage_key=self.storage_key,
                begun_at=NOW,
            ),
            handle=EphemeralHandle(
                method="PUT",
                url="https://objects.example.test/signed?secret=do-not-project",
                expires_at=NOW + (handle_ttl or timedelta(minutes=1)),
                write_once=True,
                headers={"x-upload-token": "do-not-project"},
            ),
        )

    async def finalize_upload(self, request: FinalizeArtifactUpload) -> ArtifactRecord:
        assert request.tenant_id == "tenant-a"
        return self.artifact

    async def download(self, artifact_id: UUID, *, tenant_id: str, handle_ttl=None) -> ArtifactDownload:
        assert artifact_id == self.artifact_id
        assert tenant_id == "tenant-a"
        return ArtifactDownload(
            artifact=self.artifact,
            handle=EphemeralHandle(
                method="GET",
                url="https://objects.example.test/signed?secret=do-not-project",
                expires_at=NOW + (handle_ttl or timedelta(minutes=1)),
            ),
        )

    async def commit_terminal_result(self, draft: TerminalResultDraft) -> TerminalResultManifest:
        raise NotImplementedError


def test_controller_routes_project_only_safe_artifact_pointer() -> None:
    service = StubControllerService()

    async def principal_dependency() -> Principal:
        return Principal(
            token_id=uuid4(),
            token_prefix="fst_test",
            principal_id="principal-a",
            tenant_id="tenant-a",
            scopes=frozenset({"inference.invoke"}),
            models=frozenset({"proteina-complexa"}),
        )

    app = FastAPI()
    app.include_router(
        scientific_artifact_router(
            service=service,
            principal_dependency=principal_dependency,
        )
    )
    with TestClient(app) as client:
        upload_id = uuid4()
        begin = client.post(
            "/internal/scientific-artifacts/uploads",
            json={
                "upload_id": str(upload_id),
                "operation_id": str(service.operation_id),
                "attempt": 1,
                "direction": "output",
                "sha256": "a" * 64,
                "size_bytes": 7,
                "media_type": "application/json",
            },
        )
        assert begin.status_code == 201
        assert set(begin.json()) == {"upload_id", "handle"}
        assert "storage_key" not in begin.text
        assert "tenant_id" not in begin.text

        finalized = client.post(
            f"/internal/scientific-artifacts/uploads/{upload_id}:finalize",
            json={"operation_id": str(service.operation_id), "attempt": 1},
        )
        assert finalized.status_code == 200
        assert finalized.json() == {
            "artifact_id": str(service.artifact_id),
            "sha256": "a" * 64,
            "size_bytes": 7,
            "media_type": "application/json",
        }
        assert "storage_key" not in finalized.text
        assert "tenant-a" not in finalized.text

        downloaded = client.get(f"/internal/scientific-artifacts/{service.artifact_id}:download")
        assert downloaded.status_code == 200
        assert downloaded.json()["artifact"] == finalized.json()
        assert "storage_key" not in downloaded.json()["artifact"]
        assert "tenant-a" not in downloaded.json()["artifact"]
        assert "secret=do-not-project" in downloaded.json()["handle"]["url"]
