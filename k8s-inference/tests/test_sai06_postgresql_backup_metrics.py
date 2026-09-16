from __future__ import annotations

import importlib.util
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "stages/workloads/scripts/postgresql_backup_metrics.py"
SPEC = importlib.util.spec_from_file_location("postgresql_backup_metrics", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
METRICS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(METRICS)


def test_version_inventory_counts_current_noncurrent_and_restore_receipt() -> None:
    pages = [
        {
            "Versions": [
                {
                    "Key": "postgresql/v1/base/one",
                    "Size": 100,
                    "IsLatest": True,
                    "LastModified": datetime(2026, 9, 16, 2, tzinfo=UTC),
                },
                {
                    "Key": "postgresql/v1/base/one",
                    "Size": 80,
                    "IsLatest": False,
                    "LastModified": datetime(2026, 9, 15, 2, tzinfo=UTC),
                },
                {
                    "Key": (
                        "postgresql/v1/restore-verification/success/"
                        "sai06-20260916T030000Z.json"
                    ),
                    "Size": 16,
                    "IsLatest": True,
                    "LastModified": datetime(2026, 9, 16, 3, tzinfo=UTC),
                },
            ],
            "DeleteMarkers": [
                {
                    "Key": "postgresql/v1/old",
                    "IsLatest": True,
                    "LastModified": datetime(2026, 9, 14, tzinfo=UTC),
                }
            ],
        }
    ]

    summary = METRICS.summarize_versions(
        pages,
        prefix="postgresql/v1/",
        capacity_bytes=400,
    )

    assert summary["usage_bytes"] == 196
    assert summary["current_bytes"] == 116
    assert summary["noncurrent_bytes"] == 80
    assert summary["version_count"] == 3
    assert summary["delete_marker_count"] == 1
    assert summary["usage_ratio"] == 0.49
    assert summary["restore_last_success_timestamp_seconds"] == 0
    assert METRICS.newest_restore_receipt_key(pages, prefix="postgresql/v1/") is None


def test_restore_receipt_is_content_addressed_scoped_and_fresh() -> None:
    completed = datetime(2026, 9, 16, 3, tzinfo=UTC)
    subject = {
        "project_id": "project-e00rene",
        "region": "eu-north1",
        "bucket_name": "fs2-postgresql-backup",
        "server_name": "fs2-control-db",
        "source_commit": "a" * 40,
        "run_id": "r20260916",
        "source_backup_name": "backup-20260916",
        "source_backup_time": "2026-09-16T02:00:00Z",
        "marker_id": "sai06-20260916-a1b2c3d4",
        "target_time": "2026-09-16T02:30:00Z",
        "publisher_access_key_id": "AJE000POSTGRESQLRECEIPT",
    }
    binding = hashlib.sha256(
        json.dumps(subject, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt = {
        "schema": "fs2-serve.nebius.ai/postgresql-restore-verification/v2",
        "status": "passed",
        "subject": subject,
        "verification_binding_sha256": binding,
        "nonce": "b" * 64,
        "completed_at": "2026-09-16T03:00:00Z",
        "valid_until": "2026-09-24T03:00:00Z",
    }
    body = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    key = (
        "postgresql/v1/restore-verification/success/"
        + hashlib.sha256(body).hexdigest()
        + ".json"
    )
    assert (
        METRICS.validate_restore_receipt(
            key,
            body,
            prefix="postgresql/v1/",
            project_id="project-e00rene",
            region="eu-north1",
            bucket_name="fs2-postgresql-backup",
            server_name="fs2-control-db",
            publisher_access_key_id="AJE000POSTGRESQLRECEIPT",
            now=completed,
        )
        == completed.timestamp()
    )

    forged = json.loads(body)
    forged["subject"]["project_id"] = "project-attacker"
    forged_body = json.dumps(forged, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ValueError, match="content|scope|unbound"):
        METRICS.validate_restore_receipt(
            key,
            forged_body,
            prefix="postgresql/v1/",
            project_id="project-e00rene",
            region="eu-north1",
            bucket_name="fs2-postgresql-backup",
            server_name="fs2-control-db",
            publisher_access_key_id="AJE000POSTGRESQLRECEIPT",
            now=completed,
        )


def test_metrics_output_is_numeric_and_never_contains_object_keys() -> None:
    summary = {
        "usage_bytes": 196,
        "current_bytes": 116,
        "noncurrent_bytes": 80,
        "capacity_bytes": 400,
        "usage_ratio": 0.49,
        "version_count": 3,
        "delete_marker_count": 1,
        "restore_last_success_timestamp_seconds": 1_789_527_600.0,
    }

    rendered = METRICS.render_metrics(
        summary,
        scrape_success=True,
        last_success_timestamp=1_789_527_601.0,
        errors_total=0,
    )

    assert "fs2_postgresql_backup_bucket_usage_bytes 196" in rendered
    assert "fs2_postgresql_backup_bucket_scrape_success 1" in rendered
    assert "fs2_postgresql_restore_last_success_timestamp_seconds" in rendered
    assert "postgresql/v1" not in rendered
    assert "sai06-20260916" not in rendered
