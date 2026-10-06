"""Bounded S3 multipart control; bytes travel directly from caller to storage.

The database-owned artifact intent supplies the key/type/expected size. S3 owns
durable part state. Repeated start discovers the same key's unfinished upload;
list/part signing lets a disconnected caller continue without retransmission.
Only the existing artifact finalizer may publish the verified SHA-256 identity.
"""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator

from .models import Principal, StrictModel
from .scientific_artifacts import ArtifactPolicyError, ArtifactVerificationError

PART_BYTES = 64 * 1024**2
MIN_PART_BYTES = 5 * 1024**2


class MultipartCommand(StrictModel):
    operation_id: UUID
    action: Literal["start", "list", "parts", "complete", "abort"]
    multipart_upload_id: str | None = Field(default=None, min_length=1, max_length=2048)
    part_numbers: list[Annotated[int, Field(ge=1, le=10000)]] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def check_action(self):
        if (self.action == "start") != (self.multipart_upload_id is None):
            raise ValueError("start omits multipart_upload_id; other actions require it")
        if (self.action == "parts") != bool(self.part_numbers) or len(set(self.part_numbers)) != len(self.part_numbers):
            raise ValueError("parts requires distinct part_numbers; other actions omit them")
        return self


def operate(
    client: Any,
    *,
    bucket: str,
    storage_key: str,
    media_type: str,
    compression: str | None,
    expected_size_bytes: int,
    command: MultipartCommand,
) -> dict:
    """Run in an executor; boto3 handles provider error responses and retries."""
    bound = {"Bucket": bucket, "Key": storage_key}
    if command.action == "start":
        # Exact-match the intent key: a prefix is not an authorization boundary.
        existing = []
        for page in client.get_paginator("list_multipart_uploads").paginate(Bucket=bucket, Prefix=storage_key):
            existing.extend(row for row in page.get("Uploads", []) if row["Key"] == storage_key)
        if existing:
            upload_id = min(existing, key=lambda row: row["Initiated"])["UploadId"]
        else:
            options = {**bound, "ContentType": media_type}
            if compression is not None:
                options["ContentEncoding"] = compression
            upload_id = client.create_multipart_upload(**options)["UploadId"]
        return {
            "multipart_upload_id": upload_id,
            "part_size_bytes": PART_BYTES,
            "expected_size_bytes": expected_size_bytes,
            "status": "uploading",
        }
    bound["UploadId"] = command.multipart_upload_id
    if command.action == "parts":
        if any((number - 1) * MIN_PART_BYTES >= expected_size_bytes for number in command.part_numbers):
            raise ArtifactPolicyError("part number exceeds this upload's expected size")
        return {
            "parts": [
                {
                    "part_number": number,
                    "method": "PUT",
                    "headers": {},
                    "expires_in_seconds": 900,
                    "url": client.generate_presigned_url(
                        "upload_part", Params={**bound, "PartNumber": number}, ExpiresIn=900
                    ),
                }
                for number in command.part_numbers
            ]
        }
    if command.action == "abort":
        client.abort_multipart_upload(**bound)
        return {"status": "aborted"}
    parts = []
    for page in client.get_paginator("list_parts").paginate(**bound):
        parts.extend(page.get("Parts", []))
        if len(parts) > 10000:
            raise ArtifactPolicyError("multipart upload exceeds 10000 parts")
    parts.sort(key=lambda part: part["PartNumber"])
    public = [{"part_number": part["PartNumber"], "size_bytes": part["Size"], "etag": part["ETag"]} for part in parts]
    if command.action == "list":
        return {"status": "uploading", "parts": public, "uploaded_bytes": sum(p["Size"] for p in parts)}
    if (
        not parts
        or sum(part["Size"] for part in parts) != expected_size_bytes
        or any(
            part["PartNumber"] != index + 1 or (index < len(parts) - 1 and part["Size"] < MIN_PART_BYTES)
            for index, part in enumerate(parts)
        )
    ):
        raise ArtifactVerificationError("multipart parts do not match the reserved size or contiguous order")
    client.complete_multipart_upload(
        **bound, MultipartUpload={"Parts": [{"PartNumber": part["PartNumber"], "ETag": part["ETag"]} for part in parts]}
    )
    return {"status": "uploaded", "size_bytes": expected_size_bytes, "finalized": False}


def multipart_router(runtime, principal_dependency) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/scientific-artifacts/uploads/{upload_id}/multipart")
    async def multipart(
        upload_id: UUID, payload: MultipartCommand, identity: Annotated[Principal, Depends(principal_dependency)]
    ):
        service = runtime.scientific_input_uploads
        if service is None:
            raise HTTPException(503, "scientific input upload is disabled")
        # Reuse the same tenant/model/operation ownership as the small-file path.
        await service._authorize(identity, payload.operation_id, upload_id)
        from .scientific_artifacts import FinalizeArtifactUpload

        result = await service.artifacts.multipart_upload(
            FinalizeArtifactUpload(
                upload_id=upload_id, operation_id=payload.operation_id, tenant_id=identity.tenant_id
            ),
            payload.model_dump(mode="json"),
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return router
