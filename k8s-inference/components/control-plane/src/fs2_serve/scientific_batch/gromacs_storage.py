"""Export closed native MD segments with the existing customer's scoped S3 key.

Only the trusted artifact companion receives credentials, in memory, through
the current attempt capability. The simulation container never receives them.
S3Transfer provides bounded-memory multipart uploads/retries. A generation's
manifest is written last; content-addressed objects never overwrite old data.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import boto3
import httpx
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError
from fs2_gromacs.files import digest_file

from .companion import WorkloadArtifactHttpClient
from .native_workflows import NativeWorkflow, workflow_for_collector, workflow_for_schema


def _verified_metadata(head: dict[str, Any], digest: str, size: int) -> bool:
    # S3 user metadata is carried in case-insensitive HTTP headers. Nebius
    # returns "Sha256" in HEAD responses; other providers use "sha256".
    # Reject conflicting aliases rather than silently choosing one of them.
    digests = [value for key, value in head.get("Metadata", {}).items() if key.lower() == "sha256"]
    return head.get("ContentLength") == size and bool(digests) and all(value == digest for value in digests)


def _provider_failure(error: BaseException) -> str:
    """Retain useful S3 diagnostics without printing URLs, headers or credentials."""
    description = type(error).__name__
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ClientError):
            code = str(current.response.get("Error", {}).get("Code", "unknown"))
            status = current.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
                description += f" code={code}"
            if type(status) is int:
                description += f" HTTP={status}"
            break
        current = current.__cause__ or current.__context__
    return description


class GromacsCustomerStorage:
    """Shared exporter with a compatibility name for the original GROMACS App."""

    def __init__(
        self,
        client: WorkloadArtifactHttpClient,
        workspace: Path,
        operation: str,
        job: str,
        *,
        workflow: NativeWorkflow | None = None,
    ) -> None:
        self.client, self.workspace, self.operation, self.job = client, workspace, operation, job
        self.workflow = workflow or workflow_for_collector("gromacs-workflow-v1")
        self.bound_model = workflow.model_id if workflow is not None else None
        if self.workflow is None:
            raise ValueError("native customer storage workflow is not registered")
        self.s3: Any = None
        self.bucket = ""
        self.prefix = ""
        self.attempt = 0
        self.enabled = False
        self.transfer = TransferConfig(
            multipart_threshold=64 * 1024**2,
            multipart_chunksize=64 * 1024**2,
            max_concurrency=2,
            max_io_queue=4,
            io_chunksize=1024**2,
        )

    def initialize(self) -> None:
        raw = json.loads((self.workspace / ".fs2/request.json").read_text())
        workflow = workflow_for_schema(raw.get("schema", ""))
        if workflow is None or (self.bound_model is not None and self.bound_model != workflow.model_id):
            raise ValueError("customer export request differs from the native workflow binding")
        self.workflow = workflow
        request = workflow.normalize(raw)
        self.enabled = request["output_destination"] == "customer-bucket"
        if not self.enabled:
            return

        def read(response: httpx.Response) -> dict[str, Any]:
            response.read()
            return cast(dict[str, Any], response.json())

        value = self.client._download_get(
            self.client.base_url + workflow.storage_endpoint,
            headers=self.client.headers,
            read=read,
        )
        self.bucket = value["bucket_name"]
        self.attempt = value["attempt_number"]
        self.prefix = f"{request['output_prefix']}/{self.operation}/{self.job}"
        self.s3 = boto3.client(
            "s3",
            endpoint_url=value["endpoint"],
            region_name=value["region"],
            aws_access_key_id=value["access_key_id"],
            aws_secret_access_key=value["secret_access_key"],
            config=Config(
                signature_version="s3v4",
                retries={"mode": "standard", "max_attempts": 5},
                s3={"addressing_style": "path"},
            ),
        )
        # Do not persist credentials in a workspace, artifact, result or log.
        value.clear()

    def _put_file(self, source: Path, digest: str, size: int) -> str:
        key = f"{self.prefix}/objects/{digest}"
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise RuntimeError("customer bucket is unavailable; checkpoint export was not committed") from None
        else:
            if not _verified_metadata(head, digest, size):
                raise ValueError("customer checkpoint object has conflicting metadata")
            return key
        if source.stat().st_size != size or digest_file(source) != digest:
            raise ValueError("customer export source changed after the engine stopped")
        try:
            self.s3.upload_file(
                str(source),
                self.bucket,
                key,
                Config=self.transfer,
                ExtraArgs={"Metadata": {"sha256": digest}, "ContentType": "application/octet-stream"},
            )
            head = self.s3.head_object(Bucket=self.bucket, Key=key)
        except Exception as error:
            # Provider errors may contain keys/headers. Give the user a useful
            # failure without leaking credential material into Job logs.
            raise RuntimeError(
                "customer checkpoint export failed; check bucket availability and quota: " + _provider_failure(error)
            ) from None
        if not _verified_metadata(head, digest, size):
            raise ValueError("customer checkpoint export size/digest metadata did not verify")
        return key

    def publish(self, state: dict[str, Any], files: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        exported = []
        for item in files:
            key = self._put_file(self.workspace / "data" / item["path"], item["sha256"], item["size_bytes"])
            exported.append(
                {"path": item["path"], "key": key, "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
            )
        key = f"{self.prefix}/attempt-{self.attempt:03d}/checkpoint-{state['generation']:08d}.json"
        manifest = {
            "schema": self.workflow.customer_checkpoint_schema,
            "state": state,
            "bucket": self.bucket,
            "files": exported,
            "retention": "customer-managed",
        }
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(manifest, sort_keys=True).encode(),
            ContentType="application/json",
        )
        return {"bucket": self.bucket, "manifest_key": key, "prefix": self.prefix, "retention": "customer-managed"}


NativeCustomerStorage = GromacsCustomerStorage
