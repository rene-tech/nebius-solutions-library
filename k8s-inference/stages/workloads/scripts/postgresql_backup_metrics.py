#!/usr/bin/env python3
"""Expose payload-free S3 version/capacity metrics for the CNPG backup bucket."""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any

RESTORE_RECEIPT_PREFIX = "restore-verification/success/"


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
    restore_last_success = 0.0
    receipt_prefix = f"{prefix.rstrip('/')}/{RESTORE_RECEIPT_PREFIX}"
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
                if key.startswith(receipt_prefix):
                    modified = version.get("LastModified")
                    if isinstance(modified, datetime):
                        restore_last_success = max(
                            restore_last_success, modified.timestamp()
                        )
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
        "restore_last_success_timestamp_seconds": restore_last_success,
    }


def render_metrics(
    summary: Mapping[str, int | float],
    *,
    scrape_success: bool,
    last_success_timestamp: float,
    errors_total: int,
) -> str:
    values = {
        "fs2_postgresql_backup_bucket_usage_bytes": summary.get("usage_bytes", 0),
        "fs2_postgresql_backup_bucket_current_bytes": summary.get(
            "current_bytes", 0
        ),
        "fs2_postgresql_backup_bucket_noncurrent_bytes": summary.get(
            "noncurrent_bytes", 0
        ),
        "fs2_postgresql_backup_bucket_capacity_bytes": summary.get(
            "capacity_bytes", 0
        ),
        "fs2_postgresql_backup_bucket_usage_ratio": summary.get("usage_ratio", 0),
        "fs2_postgresql_backup_bucket_object_versions": summary.get(
            "version_count", 0
        ),
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
        self._summary: dict[str, int | float] = {
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
                    pages = self._client.get_paginator("list_object_versions").paginate(
                        Bucket=os.environ["S3_BUCKET"],
                        Prefix=os.environ["S3_PREFIX"],
                    )
                    self._summary = summarize_versions(
                        pages,
                        prefix=os.environ["S3_PREFIX"],
                        capacity_bytes=int(os.environ["BUCKET_CAPACITY_BYTES"]),
                    )
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
