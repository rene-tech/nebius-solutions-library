#!/usr/bin/env python3
"""Expose payload-free S3 version/capacity metrics for the CNPG backup bucket."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any, cast

RESTORE_RECEIPT_PREFIX = "restore-verification/success/"
RECEIPT_FIELDS = {
    "schema",
    "status",
    "subject",
    "verification_binding_sha256",
    "nonce",
    "completed_at",
    "valid_until",
}
RECEIPT_SUBJECT_FIELDS = {
    "project_id",
    "region",
    "bucket_name",
    "server_name",
    "source_commit",
    "source_tree",
    "run_id",
    "source_cluster_uid",
    "source_backup_name",
    "source_backup_uid",
    "source_backup_time",
    "source_backup_wal",
    "verified_wal",
    "marker_id",
    "target_time",
    "publisher_access_key_id",
}


def _utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("receipt timestamp is not UTC Z time")
    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(UTC)


def newest_restore_receipt_key(
    pages: Iterable[Mapping[str, Any]], *, prefix: str
) -> str | None:
    receipt_prefix = f"{prefix.rstrip('/')}/{RESTORE_RECEIPT_PREFIX}"
    candidates: list[tuple[datetime, str]] = []
    for page in pages:
        for version in page.get("Versions", []):
            key = str(version.get("Key", ""))
            modified = version.get("LastModified")
            if (
                version.get("IsLatest") is True
                and re.fullmatch(re.escape(receipt_prefix) + r"[0-9a-f]{64}\.json", key)
                and isinstance(modified, datetime)
            ):
                candidates.append((modified, key))
    return max(candidates)[1] if candidates else None


def validate_restore_receipt(
    key: str,
    body: bytes,
    *,
    prefix: str,
    project_id: str,
    region: str,
    bucket_name: str,
    server_name: str,
    publisher_access_key_id: str,
    now: datetime | None = None,
) -> dict[str, str | float]:
    """Validate exact receipt content and its content-addressed immutable key."""
    if len(body) == 0 or len(body) > 8192:
        raise ValueError("restore receipt has an invalid size")
    expected_key = (
        f"{prefix.rstrip('/')}/{RESTORE_RECEIPT_PREFIX}"
        f"{hashlib.sha256(body).hexdigest()}.json"
    )
    if key != expected_key:
        raise ValueError("restore receipt key is not bound to its exact content")
    document = json.loads(body)
    if not isinstance(document, dict) or set(document) != RECEIPT_FIELDS:
        raise ValueError("restore receipt fields are not exact")
    if (
        document["schema"] != "fs2-serve.nebius.ai/postgresql-restore-verification/v2"
        or document["status"] != "passed"
    ):
        raise ValueError("restore receipt schema/status is invalid")
    subject = document["subject"]
    if not isinstance(subject, dict) or set(subject) != RECEIPT_SUBJECT_FIELDS:
        raise ValueError("restore receipt subject fields are not exact")
    if (
        subject["project_id"] != project_id
        or subject["region"] != region
        or subject["bucket_name"] != bucket_name
        or subject["server_name"] != server_name
        or subject["publisher_access_key_id"] != publisher_access_key_id
    ):
        raise ValueError("restore receipt scope is invalid")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", str(subject["source_commit"]))
        or not re.fullmatch(r"[0-9a-f]{40}", str(subject["source_tree"]))
        or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            str(subject["source_cluster_uid"]),
        )
        or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            str(subject["source_backup_uid"]),
        )
        or not re.fullmatch(r"[a-z][a-z0-9]{5,11}", str(subject["run_id"]))
        or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{7,62}", str(subject["source_backup_name"])
        )
        or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{7,62}", str(subject["marker_id"])
        )
        or not re.fullmatch(
            r"[0-9A-F]{24}", str(subject["source_backup_wal"])
        )
        or not re.fullmatch(r"[0-9A-F]{24}", str(subject["verified_wal"]))
        or int(str(subject["source_backup_wal"]), 16) == 0
        or int(str(subject["verified_wal"]), 16)
        <= int(str(subject["source_backup_wal"]), 16)
        or not re.fullmatch(r"[0-9a-f]{64}", str(document["nonce"]))
    ):
        raise ValueError("restore receipt source or nonce is invalid")
    canonical_subject = json.dumps(
        subject, sort_keys=True, separators=(",", ":")
    ).encode()
    if (
        document["verification_binding_sha256"]
        != hashlib.sha256(canonical_subject).hexdigest()
    ):
        raise ValueError("restore receipt verification subject is unbound")
    completed = _utc(document["completed_at"])
    valid_until = _utc(document["valid_until"])
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if completed > current + timedelta(minutes=5):
        raise ValueError("restore receipt completion is in the future")
    if (
        current - completed > timedelta(hours=36)
        or valid_until <= current
        or valid_until > completed + timedelta(hours=36)
    ):
        raise ValueError("restore receipt is expired or overlong")
    source_backup_time = _utc(subject["source_backup_time"])
    target_time = _utc(subject["target_time"])
    if not source_backup_time < target_time <= completed:
        raise ValueError("restore receipt PITR time ordering is invalid")
    return {
        "completed_timestamp_seconds": completed.timestamp(),
        "cluster_uid": str(subject["source_cluster_uid"]),
        "backup_name": str(subject["source_backup_name"]),
        "backup_uid": str(subject["source_backup_uid"]),
        "source_commit": str(subject["source_commit"]),
        "source_tree": str(subject["source_tree"]),
        "run_id": str(subject["run_id"]),
        "source_backup_wal": str(subject["source_backup_wal"]),
        "verified_wal": str(subject["verified_wal"]),
    }


def summarize_versions(
    pages: Iterable[Mapping[str, Any]],
    *,
    prefix: str,
    capacity_bytes: int,
) -> dict[str, int | float]:
    current_bytes = 0
    noncurrent_bytes = 0
    version_count = 0
    delete_marker_count = 0
    for page in pages:
        for version in page.get("Versions", []):
            key = str(version.get("Key", ""))
            if not key.startswith(prefix):
                continue
            size = int(version.get("Size", 0))
            if size < 0:
                raise ValueError("S3 returned a negative object-version size")
            version_count += 1
            if version.get("IsLatest") is True:
                current_bytes += size
            else:
                noncurrent_bytes += size
        delete_marker_count += sum(
            1
            for marker in page.get("DeleteMarkers", [])
            if str(marker.get("Key", "")).startswith(prefix)
        )
    usage_bytes = current_bytes + noncurrent_bytes
    return {
        "usage_bytes": usage_bytes,
        "current_bytes": current_bytes,
        "noncurrent_bytes": noncurrent_bytes,
        "capacity_bytes": capacity_bytes,
        "usage_ratio": usage_bytes / capacity_bytes,
        "version_count": version_count,
        "delete_marker_count": delete_marker_count,
        "restore_last_success_timestamp_seconds": 0.0,
    }


def render_metrics(
    summary: Mapping[str, Any],
    *,
    scrape_success: bool,
    last_success_timestamp: float,
    errors_total: int,
) -> str:
    values = {
        "fs2_postgresql_backup_bucket_usage_bytes": summary.get("usage_bytes", 0),
        "fs2_postgresql_backup_bucket_current_bytes": summary.get("current_bytes", 0),
        "fs2_postgresql_backup_bucket_noncurrent_bytes": summary.get(
            "noncurrent_bytes", 0
        ),
        "fs2_postgresql_backup_bucket_capacity_bytes": summary.get("capacity_bytes", 0),
        "fs2_postgresql_backup_bucket_usage_ratio": summary.get("usage_ratio", 0),
        "fs2_postgresql_backup_bucket_object_versions": summary.get("version_count", 0),
        "fs2_postgresql_backup_bucket_delete_markers": summary.get(
            "delete_marker_count", 0
        ),
        "fs2_postgresql_restore_last_success_timestamp_seconds": summary.get(
            "restore_last_success_timestamp_seconds", 0
        ),
        "fs2_postgresql_backup_bucket_scrape_success": 1 if scrape_success else 0,
        "fs2_postgresql_backup_bucket_last_success_timestamp_seconds": (
            last_success_timestamp
        ),
        "fs2_postgresql_backup_bucket_errors_total": errors_total,
    }
    lines: list[str] = []
    for name, value in values.items():
        metric_type = "counter" if name.endswith("_total") else "gauge"
        lines.extend((f"# TYPE {name} {metric_type}", f"{name} {value}"))
    receipt_labels = summary.get("restore_receipt_labels")
    if isinstance(receipt_labels, Mapping):
        label_names = (
            "backup_name",
            "backup_uid",
            "cluster_uid",
            "run_id",
            "source_backup_wal",
            "source_commit",
            "source_tree",
            "verified_wal",
        )
        if set(receipt_labels) == set(label_names):
            labels = ",".join(
                f'{name}="{receipt_labels[name]}"' for name in label_names
            )
            lines.extend(
                (
                    "# TYPE fs2_postgresql_restore_receipt_info gauge",
                    f"fs2_postgresql_restore_receipt_info{{{labels}}} 1",
                )
            )
    return "\n".join(lines) + "\n"


def build_client() -> Any:
    # The pinned CloudNativePG system image carries Barman Cloud's boto3
    # dependency. Credentials are injected from the MysteryBox-delivered S3
    # Secret and are never rendered into metrics or logs.
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT"],
        region_name=os.environ["AWS_REGION"],
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    )


class Inventory:
    def __init__(self) -> None:
        self._lock = Lock()
        self._client: Any | None = None
        self._summary: dict[str, Any] = {
            "capacity_bytes": int(os.environ["BUCKET_CAPACITY_BYTES"])
        }
        self._last_attempt = 0.0
        self._last_success = 0.0
        self._errors = 0
        self._success = False

    def metrics(self) -> str:
        now = time.time()
        with self._lock:
            if now - self._last_attempt >= 300:
                self._last_attempt = now
                try:
                    if self._client is None:
                        self._client = build_client()
                    pages = list(
                        self._client.get_paginator("list_object_versions").paginate(
                            Bucket=os.environ["S3_BUCKET"],
                            Prefix=os.environ["S3_PREFIX"],
                        )
                    )
                    self._summary = cast(
                        dict[str, Any],
                        summarize_versions(
                            pages,
                            prefix=os.environ["S3_PREFIX"],
                            capacity_bytes=int(os.environ["BUCKET_CAPACITY_BYTES"]),
                        ),
                    )
                    receipt_key = newest_restore_receipt_key(
                        pages, prefix=os.environ["S3_PREFIX"]
                    )
                    if receipt_key is not None:
                        response = self._client.get_object(
                            Bucket=os.environ["S3_BUCKET"], Key=receipt_key
                        )
                        body = response["Body"].read(8193)
                        receipt = validate_restore_receipt(
                            receipt_key,
                            body,
                            prefix=os.environ["S3_PREFIX"],
                            project_id=os.environ["PROJECT_ID"],
                            region=os.environ["AWS_REGION"],
                            bucket_name=os.environ["S3_BUCKET"],
                            server_name=os.environ["SERVER_NAME"],
                            publisher_access_key_id=os.environ[
                                "RECEIPT_PUBLISHER_ACCESS_KEY_ID"
                            ],
                        )
                        self._summary["restore_last_success_timestamp_seconds"] = (
                            float(receipt["completed_timestamp_seconds"])
                        )
                        self._summary["restore_receipt_labels"] = {
                            name: str(receipt[name])
                            for name in (
                                "backup_name",
                                "backup_uid",
                                "cluster_uid",
                                "run_id",
                                "source_backup_wal",
                                "source_commit",
                                "source_tree",
                                "verified_wal",
                            )
                        }
                    self._last_success = now
                    self._success = True
                except Exception:  # the failure is exported, never logged with secrets
                    self._errors += 1
                    self._success = False
            return render_metrics(
                self._summary,
                scrape_success=self._success,
                last_success_timestamp=self._last_success,
                errors_total=self._errors,
            )


INVENTORY: Inventory | None = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path == "/healthz":
            body = b"ok\n"
            status = 200
            content_type = "text/plain; charset=utf-8"
        elif self.path == "/metrics":
            assert INVENTORY is not None
            body = INVENTORY.metrics().encode()
            status = 200
            content_type = "text/plain; version=0.0.4; charset=utf-8"
        else:
            body = b"not found\n"
            status = 404
            content_type = "text/plain; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    global INVENTORY
    INVENTORY = Inventory()
    # The Pod ServiceMonitor reaches this isolated container over its Pod IP.
    ThreadingHTTPServer(("0.0.0.0", 9188), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
