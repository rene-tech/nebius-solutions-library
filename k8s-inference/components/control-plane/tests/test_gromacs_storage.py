import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI

from fs2_serve.scientific_batch.gromacs_storage import GromacsCustomerStorage, _provider_failure, _verified_metadata
from fs2_serve.scientific_batch.gromacs_storage_routes import gromacs_storage_router
from fs2_serve.scientific_batch.native_workflows import workflow_for_collector
from fs2_serve.user_storage_models import StorageCredentials


class S3:
    def __init__(self, metadata_case=str.lower):
        self.objects = {}
        self.writes = []
        self.metadata_case = metadata_case

    def head_object(self, *, Bucket, Key):  # noqa: N803 - boto3's public API
        if (Bucket, Key) not in self.objects:
            raise ClientError({"ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        body, metadata = self.objects[Bucket, Key]
        return {
            "ContentLength": len(body),
            "Metadata": {self.metadata_case(key): value for key, value in metadata.items()},
        }

    def upload_file(self, filename, bucket, key, *, Config, ExtraArgs):  # noqa: N803
        assert Config.max_concurrency == 2 and Config.multipart_chunksize == 64 * 1024**2
        self.objects[bucket, key] = (Path(filename).read_bytes(), ExtraArgs["Metadata"])
        self.writes.append(key)

    def put_object(self, *, Bucket, Key, Body, ContentType):  # noqa: N803
        self.objects[Bucket, Key] = (Body, {})
        self.writes.append(Key)


def test_nested_provider_failure_retains_code_but_not_secret_message():
    inner = ClientError(
        {"Error": {"Code": "SlowDown", "Message": "secret-url-and-key"}, "ResponseMetadata": {"HTTPStatusCode": 503}},
        "UploadPart",
    )
    outer = RuntimeError("wrapped secret-url-and-key")
    outer.__cause__ = inner
    assert _provider_failure(outer) == "RuntimeError code=SlowDown HTTP=503"


def test_unstructured_provider_error_does_not_leak_its_message():
    assert _provider_failure(RuntimeError("secret-url-and-key")) == "RuntimeError"


@pytest.mark.parametrize("metadata_case", [str.lower, str.title, str.upper])
@pytest.mark.parametrize("engine", ["gromacs", "lammps", "namd", "amber"])
def test_export_preserves_files_and_publishes_manifest_last_without_reupload(tmp_path, metadata_case, engine):
    (tmp_path / "data").mkdir()
    path = tmp_path / "data/md.part0001.xtc"
    raw = b"native trajectory"
    path.write_bytes(raw)
    item = {"path": path.name, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
    workflow = workflow_for_collector(f"{engine}-workflow-v1")
    export = GromacsCustomerStorage(None, tmp_path, "operation", "replica", workflow=workflow)
    export.s3, export.bucket, export.prefix, export.attempt, export.enabled = (
        S3(metadata_case),
        "customer",
        "runs/op/replica",
        2,
        True,
    )
    first = export.publish({"generation": 3}, [item])
    second = export.publish({"generation": 4}, [item])
    assert len(export.s3.writes) == 3  # one object, two commit manifests
    assert first["manifest_key"].endswith("attempt-002/checkpoint-00000003.json")
    raw_manifest, _ = export.s3.objects["customer", second["manifest_key"]]
    manifest = json.loads(raw_manifest)
    assert manifest["retention"] == "customer-managed"
    assert manifest["schema"] == workflow.customer_checkpoint_schema
    assert manifest["files"][0]["path"] == path.name
    assert not any("secret" in key for key in manifest)


@pytest.mark.parametrize(
    "head",
    [
        {"ContentLength": 10, "Metadata": {}},
        {"ContentLength": 11, "Metadata": {"Sha256": "correct"}},
        {"ContentLength": 10, "Metadata": {"Sha256": "incorrect"}},
        {"ContentLength": 10, "Metadata": {"sha256": "correct", "Sha256": "incorrect"}},
    ],
)
def test_metadata_case_does_not_mask_missing_or_conflicting_integrity(head):
    assert not _verified_metadata(head, "correct", 10)


def test_export_failure_never_commits_a_customer_manifest(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/native.cpt").write_bytes(b"checkpoint")
    export = GromacsCustomerStorage(None, tmp_path, "operation", "replica")
    export.s3, export.bucket, export.prefix, export.attempt, export.enabled = (
        S3(),
        "customer",
        "runs/op/replica",
        1,
        True,
    )

    def fail(*args, **kwargs):
        raise RuntimeError("provider message containing secrets must not be logged")

    monkeypatch.setattr(export.s3, "upload_file", fail)
    with pytest.raises(RuntimeError, match="check bucket availability and quota"):
        export.publish(
            {"generation": 1},
            [{"path": "native.cpt", "sha256": hashlib.sha256(b"checkpoint").hexdigest(), "size_bytes": 10}],
        )
    assert export.s3.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,family",
    [("gromacs", "gromacs"), ("gromacs-mpi", "gromacs"), ("lammps", "native"), ("namd", "native"), ("amber", "native")],
)
@pytest.mark.parametrize(
    "mode,ready,other_model,revoked,expected",
    [
        ("tenant", True, False, False, 200),
        ("user", True, False, False, 200),
        ("disabled", True, False, False, 409),
        ("tenant", False, False, False, 409),
        ("tenant", True, True, False, 403),
        ("tenant", True, False, True, 409),
    ],
)
async def test_destination_uses_the_original_submitter_not_request_fields(
    monkeypatch, mode, ready, other_model, revoked, expected, model, family
):
    capability = SimpleNamespace(
        model_id="other" if other_model else model,
        stage_id="workflow",
        collector_id=f"{model}-workflow-v1",
        operation_id=uuid4(),
        tenant_id="tenant-a",
        attempt_number=2,
    )
    monkeypatch.setattr(
        "fs2_serve.scientific_batch.gromacs_storage_routes.authorize_workload_capability",
        AsyncMock(return_value=(capability, None, None)),
    )
    operation = SimpleNamespace(
        model_id=model,
        tenant_id="tenant-a",
        principal_id="scientist-a",
        token_id=uuid4(),
        status=SimpleNamespace(terminal=False),
    )
    token = SimpleNamespace(
        tenant_id="tenant-a", principal_id="scientist-a", revoked_at=True if revoked else None, expires_at=None
    )
    store = SimpleNamespace(get_operation=AsyncMock(return_value=operation), get_token=AsyncMock(return_value=token))
    credential = StorageCredentials(
        bucket_name="scoped-bucket",
        endpoint="https://objects.test",
        region="test-region",
        access_key_id="test-access",
        secret_access_key="test-only-secret",
    )
    storage = SimpleNamespace(
        policy=AsyncMock(return_value=SimpleNamespace(mode=mode)),
        view=AsyncMock(return_value=SimpleNamespace(state="ready" if ready else "pending")),
        repository=SimpleNamespace(disclose=AsyncMock(return_value=credential)),
    )
    app = FastAPI()
    app.include_router(
        gromacs_storage_router(authority=None, batches=None, store=store, storage=storage, family=family)
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/internal/scientific-workloads/{family}/storage")
    assert response.status_code == expected
    if expected == 200:
        storage.repository.disclose.assert_awaited_once_with("tenant-a", "scientist-a")
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["attempt_number"] == 2
    else:
        storage.repository.disclose.assert_not_awaited()
