from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fs2_serve.scientific_artifacts import ArtifactVerificationError
from fs2_serve.scientific_multipart import MIN_PART_BYTES, MultipartCommand, multipart_router, operate


def command(action, **values):
    return MultipartCommand(
        operation_id=uuid4(),
        action=action,
        **({} if action == "start" else {"multipart_upload_id": "owned-upload"}),
        **values,
    )


def storage(action, parts=(), **values):
    client = Mock()
    client.get_paginator.return_value.paginate.return_value = [{"Parts": list(parts)}]

    def call():
        return operate(
            client,
            bucket="our-bucket",
            storage_key="tenant/intent",
            media_type="application/x-hdf5",
            compression=None,
            expected_size_bytes=MIN_PART_BYTES + 17,
            command=command(action, **values),
        )

    return client, call


def test_start_resumes_exact_key_not_neighboring_prefix():
    client = Mock()
    client.get_paginator.return_value.paginate.return_value = [
        {
            "Uploads": [
                {"Key": "tenant/intent-extra", "UploadId": "wrong", "Initiated": 0},
                {"Key": "tenant/intent", "UploadId": "existing", "Initiated": 1},
            ]
        }
    ]
    result = operate(
        client,
        bucket="b",
        storage_key="tenant/intent",
        media_type="application/x-hdf5",
        compression=None,
        expected_size_bytes=10**10,
        command=command("start"),
    )
    assert result["multipart_upload_id"] == "existing"
    client.create_multipart_upload.assert_not_called()


def test_part_signatures_bind_intent_key_and_provider_upload():
    client, call = storage("parts", part_numbers=[1, 2])
    result = call()
    assert len(result["parts"]) == 2
    assert client.generate_presigned_url.call_args.kwargs["Params"] == {
        "Bucket": "our-bucket",
        "Key": "tenant/intent",
        "UploadId": "owned-upload",
        "PartNumber": 2,
    }


def test_completion_uses_provider_measured_parts_not_client_assertions():
    parts = [{"PartNumber": 1, "Size": MIN_PART_BYTES, "ETag": "a"}, {"PartNumber": 2, "Size": 17, "ETag": "b"}]
    client, call = storage("complete", parts)
    assert call()["finalized"] is False
    client.complete_multipart_upload.assert_called_once_with(
        Bucket="our-bucket",
        Key="tenant/intent",
        UploadId="owned-upload",
        MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": "a"}, {"PartNumber": 2, "ETag": "b"}]},
    )


@pytest.mark.parametrize(
    "parts",
    [[], [{"PartNumber": 1, "Size": 5, "ETag": "a"}], [{"PartNumber": 2, "Size": MIN_PART_BYTES + 17, "ETag": "a"}]],
)
def test_incomplete_and_noncontiguous_uploads_are_not_published(parts):
    client, call = storage("complete", parts)
    with pytest.raises(ArtifactVerificationError):
        call()
    client.complete_multipart_upload.assert_not_called()


def test_list_and_abort_only_touch_the_intent():
    client, call = storage("list", [{"PartNumber": 1, "Size": 7, "ETag": "a"}])
    assert call()["uploaded_bytes"] == 7
    client.complete_multipart_upload.assert_not_called()
    client, call = storage("abort")
    assert call()["status"] == "aborted"
    client.abort_multipart_upload.assert_called_once_with(
        Bucket="our-bucket", Key="tenant/intent", UploadId="owned-upload"
    )


def test_command_bounds():
    for changes in (
        {"action": "parts", "part_numbers": []},
        {"action": "start", "multipart_upload_id": "bad"},
        {"action": "parts", "multipart_upload_id": "ok", "part_numbers": [1, 1]},
    ):
        with pytest.raises(ValidationError):
            MultipartCommand(operation_id=uuid4(), **changes)


def test_route_reuses_existing_tenant_model_and_operation_authorization():
    principal = SimpleNamespace(tenant_id="system")
    service = SimpleNamespace(
        _authorize=AsyncMock(),
        artifacts=SimpleNamespace(
            multipart_upload=AsyncMock(return_value={"status": "uploading", "multipart_upload_id": "abc"})
        ),
    )
    app = FastAPI()
    app.include_router(multipart_router(SimpleNamespace(scientific_input_uploads=service), lambda: principal))
    operation, upload = uuid4(), uuid4()
    with TestClient(app) as client:
        result = client.post(
            f"/v1/scientific-artifacts/uploads/{upload}/multipart",
            json={"operation_id": str(operation), "action": "start"},
        )
    assert result.status_code == 200, result.text
    service._authorize.assert_awaited_once_with(principal, operation, upload)
    bound = service.artifacts.multipart_upload.call_args.args[0]
    assert (bound.tenant_id, bound.operation_id, bound.upload_id) == ("system", operation, upload)
