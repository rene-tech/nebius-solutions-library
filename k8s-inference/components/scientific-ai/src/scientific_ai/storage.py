"""Streaming customer-owned objects; immutable files, commit manifest last.

No global boto3 session, metadata-service fallback, tenant static key lookup,
platform bucket copy, shared cache or deletion. The provider-enforced per-job
client is injected after authorization; SDK credential renewal stays intact.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import uuid4

from fs2_gromacs.files import FileDigestCache, file_signature

from .contracts import FileRecord, Snapshot

CHUNK = 4 * 1024**2
MANIFEST_LIMIT = 32 * 1024**2


class Objects(Protocol):
    """Already scoped to this job's authorized bucket/prefix by provider policy."""

    def put(self, key: str, source: BinaryIO) -> None: ...

    def get(self, key: str) -> BinaryIO: ...


class S3Objects:
    def __init__(self, client, *, bucket: str, prefix: str):
        from fs2_gromacs.contracts import relative_path

        if not bucket or "/" in bucket:
            raise ValueError("one authorized bucket is required")
        self.client, self.bucket = client, bucket
        self.prefix = relative_path(prefix).rstrip("/") + "/"

    def put(self, key: str, source: BinaryIO) -> None:
        # boto3 uses multipart uploads for large trajectories and refreshes its
        # configured temporary credentials; never freeze SDK credentials here.
        from boto3.s3.transfer import TransferConfig

        self.client.upload_fileobj(
            source,
            self.bucket,
            self.prefix + self._key(key),
            Config=TransferConfig(
                max_concurrency=4,
                multipart_threshold=64 * 1024**2,
                multipart_chunksize=64 * 1024**2,
            ),
        )

    def get(self, key: str) -> BinaryIO:
        return self.client.get_object(Bucket=self.bucket, Key=self.prefix + self._key(key))["Body"]

    @staticmethod
    def _key(key: str) -> str:
        from fs2_gromacs.contracts import relative_path

        return relative_path(key)


class Artifacts:
    def __init__(self, objects: Objects):
        self.objects = objects
        self.verified: set[tuple[str, int]] = set()
        self.digests = FileDigestCache()

    def verify(self, key: str, digest: str, size: int, destination: Path | None = None) -> None:
        """Verify actual bytes, not a multipart ETag or self-asserted metadata.

        Read-back is the portable S3 baseline. Backend-verified full-object
        checksums can replace it later after qualification, not by assumption.
        """
        from contextlib import ExitStack

        with ExitStack() as stack:
            source = self.objects.get(key)
            stack.callback(source.close)
            output = stack.enter_context(destination.open("xb")) if destination is not None else None
            sha, total = hashlib.sha256(), 0
            while block := source.read(CHUNK):
                total += len(block)
                if total > size:
                    raise ValueError("object exceeds its immutable size")
                sha.update(block)
                if output is not None:
                    output.write(block)
            if (total, sha.hexdigest()) != (size, digest):
                raise ValueError("object content differs from its immutable identity")

    def upload(self, path: Path, record: FileRecord) -> None:
        before = path.lstat()
        if path.is_symlink() or not path.is_file() or before.st_size != record.size_bytes:
            raise ValueError("artifact is not the declared closed regular file")
        if self.digests.digest(path) != record.sha256:
            raise ValueError("artifact changed before upload")
        identity = (record.sha256, record.size_bytes)
        if identity not in self.verified:
            key = "blobs/" + record.sha256
            with path.open("rb") as source:
                self.objects.put(key, source)
            self.verify(key, *identity)
            self.verified.add(identity)
        if file_signature(before) != file_signature(path.lstat()):
            raise ValueError("artifact changed while publishing")

    def download(self, record: FileRecord, destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            raise ValueError("restore must not overwrite an existing workspace file")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = destination.parent / (".incoming-" + uuid4().hex)
        try:
            self.verify("blobs/" + record.sha256, record.sha256, record.size_bytes, temporary)
            temporary.chmod(0o600)
            temporary.replace(destination)
        finally:
            # Only this exact incomplete task-owned file; never delete results.
            if temporary.is_file() and not temporary.is_symlink():
                temporary.unlink()

    def commit(self, snapshot: Snapshot) -> str:
        data = snapshot.model_dump_json().encode()
        if len(data) > MANIFEST_LIMIT:
            raise ValueError("snapshot manifest exceeds the size budget")
        digest = hashlib.sha256(data).hexdigest()
        self.objects.put("manifests/" + digest, io.BytesIO(data))
        self.verify("manifests/" + digest, digest, len(data))
        return digest

    def load(self, reference: str) -> Snapshot:
        from pydantic import TypeAdapter

        from .contracts import Digest

        TypeAdapter(Digest).validate_python(reference)
        source = self.objects.get("manifests/" + reference)
        try:
            data = source.read(MANIFEST_LIMIT + 1)
        finally:
            source.close()
        if len(data) > MANIFEST_LIMIT or hashlib.sha256(data).hexdigest() != reference:
            raise ValueError("snapshot manifest content differs from its reference")
        return Snapshot.model_validate(json.loads(data))
