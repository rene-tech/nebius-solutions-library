"""Bounded, create-only installation of an operator-pinned example pack.

This module never discovers cloud buckets, creates credentials, deletes objects,
or falls back to unconditional PUT. Its client must be bucket-scoped. The
durable once-per-bucket completion ledger belongs to the background service.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Any

from botocore.exceptions import ClientError

SCHEMA = "fs2-serve.nebius.ai/customer-starter-pack/v1"
MAX_PACK_BYTES = 128 * 1024 * 1024
MAX_OBJECT_BYTES = 32 * 1024 * 1024
MAX_OBJECTS = 4096
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class PackError(ValueError):
    """A bounded non-payload error code suitable for a seeding receipt."""


@dataclass(frozen=True)
class PackObject:
    path: str
    sha256: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True)
class StarterPack:
    root: Path
    version: str
    manifest: bytes
    objects: tuple[PackObject, ...]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.manifest).hexdigest()

    @property
    def total_bytes(self) -> int:
        return len(self.manifest) + sum(obj.size_bytes for obj in self.objects)

    @property
    def prefix(self) -> str:
        return f"examples/{self.version}/"

    @classmethod
    def load(cls, root: Path, *, allow_unqualified: bool = False) -> StarterPack:
        root = root.resolve(strict=True)
        raw = (root / "manifest.json").read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise PackError("manifest_too_large")
        data = json.loads(raw)
        if data.get("schema") != SCHEMA or not re.fullmatch(r"v[1-9][0-9]*", data.get("version", "")):
            raise PackError("manifest_identity_invalid")
        if not allow_unqualified and data.get("release_status") != "qualified":
            raise PackError("pack_not_qualified")
        entries = data.get("objects", [])
        if not 1 <= len(entries) <= MAX_OBJECTS:
            raise PackError("object_count_out_of_bounds")
        objects: list[PackObject] = []
        seen: set[str] = set()
        for entry in entries:
            path = entry["path"]
            relative = PurePosixPath(path)
            if (
                not path
                or relative.is_absolute()
                or ".." in relative.parts
                or "\\" in path
                or str(relative) != path
                or path in seen
                or path == "manifest.json"
                or any(ord(c) < 32 for c in path)
            ):
                raise PackError("object_path_invalid")
            seen.add(path)
            source = root / path
            if source.is_symlink() or not source.resolve(strict=True).is_relative_to(root):
                raise PackError("object_path_outside_pack")
            size = entry["size_bytes"]
            if not isinstance(size, int) or not 0 < size <= MAX_OBJECT_BYTES:
                raise PackError("object_size_out_of_bounds")
            if not SHA256.fullmatch(entry.get("sha256", "")) or not entry.get("media_type"):
                raise PackError("object_metadata_invalid")
            provenance = entry.get("provenance", {})
            if not all(provenance.get(key) for key in ("source", "license", "attribution", "transformation")):
                raise PackError("object_provenance_missing")
            if not entry.get("recipe_version") or not entry.get("validation_status"):
                raise PackError("object_validation_missing")
            content = source.read_bytes()
            if len(content) != size or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise PackError("local_object_checksum_mismatch")
            objects.append(PackObject(path, entry["sha256"], size, entry["media_type"]))
        pack = cls(root, data["version"], raw, tuple(objects))
        if pack.total_bytes > MAX_PACK_BYTES:
            raise PackError("pack_byte_budget_exceeded")
        return pack


class BucketPackInstaller:
    """Synchronous bounded I/O; call from a background thread, never a request."""

    def __init__(self, client: Any, bucket: str, *, quota_bytes: int, stop: Event | None = None) -> None:
        self.client, self.bucket, self.quota_bytes = client, bucket, quota_bytes
        self.stop = stop

    def _check_stop(self) -> None:
        if self.stop is not None and self.stop.is_set():
            raise PackError("seeding_stopped")

    def _matches(self, key: str, expected_size: int, expected_sha: str) -> bool | None:
        self._check_stop()
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
                return None
            raise
        body = response["Body"]
        try:
            if response["ContentLength"] != expected_size:
                return False
            digest, count = hashlib.sha256(), 0
            while chunk := body.read(min(1024 * 1024, expected_size + 1 - count)):
                count += len(chunk)
                if count > expected_size:
                    return False
                digest.update(chunk)
            return count == expected_size and digest.hexdigest() == expected_sha
        finally:
            body.close()

    def _usage(self) -> int:
        # Size metadata only. No unrelated object bodies or keys are retained.
        total, continuation = 0, None
        for _ in range(100):
            self._check_stop()
            options = {} if continuation is None else {"ContinuationToken": continuation}
            page = self.client.list_objects_v2(Bucket=self.bucket, MaxKeys=1000, **options)
            total += sum(obj["Size"] for obj in page.get("Contents", []))
            if not page.get("IsTruncated"):
                return total
            continuation = page.get("NextContinuationToken")
            if not continuation:
                raise PackError("bucket_inventory_invalid")
        raise PackError("bucket_inventory_budget_exceeded")

    def _put(self, key: str, content: bytes, media_type: str) -> None:
        self._check_stop()
        # A HEAD-then-PUT check alone is NOT create-only. Keep this condition on
        # every retry and fail closed if the provider does not implement it.
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=content,
                ContentType=media_type,
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                raise
        if self._matches(key, len(content), hashlib.sha256(content).hexdigest()) is not True:
            raise PackError("existing_object_conflict")

    def install(self, pack: StarterPack) -> dict[str, Any]:
        manifest_key = pack.prefix + "manifest.json"
        # Refuse conflicting manifests before installing any other objects.
        if self._matches(manifest_key, len(pack.manifest), pack.digest) is False:
            raise PackError("existing_manifest_conflict")
        missing: list[PackObject] = []
        for obj in pack.objects:
            match = self._matches(pack.prefix + obj.path, obj.size_bytes, obj.sha256)
            if match is False:
                raise PackError("existing_object_conflict")
            if match is None:
                missing.append(obj)
        # Conservative: include manifest even if already present. The provider
        # remains the final quota authority during concurrent customer writes.
        needed = sum(obj.size_bytes for obj in missing) + len(pack.manifest)
        if self._usage() + needed > self.quota_bytes:
            raise PackError("insufficient_bucket_headroom")
        for obj in missing:
            content = (pack.root / obj.path).read_bytes()
            if len(content) != obj.size_bytes or hashlib.sha256(content).hexdigest() != obj.sha256:
                raise PackError("local_object_changed")
            self._put(pack.prefix + obj.path, content, obj.media_type)
        # Verify the entire set again before committing the immutable manifest.
        for obj in pack.objects:
            if self._matches(pack.prefix + obj.path, obj.size_bytes, obj.sha256) is not True:
                raise PackError("final_verification_failed")
        self._put(manifest_key, pack.manifest, "application/json")
        return {
            "state": "complete",
            "version": pack.version,
            "manifest_sha256": pack.digest,
            "object_count": len(pack.objects) + 1,
            "total_bytes": pack.total_bytes,
            "created_objects": len(missing),
            "adopted_objects": len(pack.objects) - len(missing),
        }
