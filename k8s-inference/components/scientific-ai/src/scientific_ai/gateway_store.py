"""Customer-bucket application metadata, not an execution-state database.

S2 alone owns admission, attempts, scheduling and terminal state. These small
records bind compatibility IDs and immutable artifacts to an authenticated owner.
Conditional creates preserve identity across API restarts and concurrent retries.
"""

from __future__ import annotations

import hashlib
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from botocore.exceptions import ClientError

from .contracts import Owner
from .storage import Artifacts, S3Objects


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def identifier(owner: Owner, kind: str, value: str) -> str:
    return str(uuid5(NAMESPACE_URL, canonical([owner.model_dump(), kind, value]).decode()))


def uuid(value: str) -> str:
    return str(UUID(value))


class GatewayStore:
    def __init__(self, client, *, bucket: str, prefix: str, owner: Owner):
        self.client, self.bucket, self.owner = client, bucket, owner
        owner_hash = hashlib.sha256(canonical(owner.model_dump())).hexdigest()
        self.prefix = S3Objects(client, bucket=bucket, prefix=prefix).prefix + owner_hash + "/"

    def get(self, kind: str, identity: str) -> dict:
        try:
            response = self.client.get_object(
                Bucket=self.bucket,
                Key=f"{self.prefix}{kind}/{uuid(identity)}.json",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise KeyError(identity) from None
            raise
        with response["Body"] as body:
            value = json.loads(body.read(1024 * 1024))
        if value.get("owner") != self.owner.model_dump():
            raise PermissionError("artifact owner differs from authenticated identity")
        return value

    def save(self, kind: str, identity: str, value: dict, *, create: bool = False) -> dict:
        document = {**value, "owner": self.owner.model_dump()}
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=f"{self.prefix}{kind}/{uuid(identity)}.json",
                Body=canonical(document),
                ContentType="application/json",
                **({"IfNoneMatch": "*"} if create else {}),
            )
            return document
        except ClientError as error:
            if create and error.response["Error"]["Code"] in (
                "PreconditionFailed",
                "412",
                "ConditionalRequestConflict",
            ):
                return self.get(kind, identity)
            raise

    def objects(self, operation_id: str) -> S3Objects:
        return S3Objects(
            self.client,
            bucket=self.bucket,
            prefix=f"{self.prefix}runs/{uuid(operation_id)}",
        )

    def artifacts(self, operation_id: str) -> Artifacts:
        return Artifacts(self.objects(operation_id))

    def url(self, method: str, key: str, **params) -> str:
        return self.client.generate_presigned_url(
            method,
            Params={"Bucket": self.bucket, "Key": key, **params},
            ExpiresIn=60,
        )
